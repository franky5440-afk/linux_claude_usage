/* Claude 用量監控 Desklet
 *
 * 依 SPEC.md §4.1 讀取 state.json 並繪製三個區塊：
 * - limits：橫向進度條顯示額度百分比與重置時間
 * - cost：今日 / 本週美金估算（標註「參考估算」）
 * - projects：Top 5 專案 token 佔比
 *
 * 使用 GLib.spawn_async 非同步呼叫 collector，不阻塞主執行緒。
 */

const St = imports.gi.St;
const Clutter = imports.gi.Clutter;
const GLib = imports.gi.GLib;
const Gio = imports.gi.Gio;
const Mainloop = imports.mainloop;
const Desklet = imports.ui.desklet;
const Settings = imports.ui.settings;

// 預設寬度（設定值讀不到時的退路）
const DEFAULT_DESKLET_WIDTH = 320;
// 進度條寬度 = desklet 寬度扣掉左右內距，需與 stylesheet.css 的 .claude-usage-root 一致
const ROOT_HORIZONTAL_PADDING = 20;  // stylesheet .claude-usage-root padding 10px × 2
const DEFAULT_BAR_WIDTH = DEFAULT_DESKLET_WIDTH - ROOT_HORIZONTAL_PADDING;
const Pango = imports.gi.Pango;

/**
 * 解析 state.json 檔案
 * @param {string} path - 檔案路徑
 * @returns {Object|null} 解析後的物件，失敗回傳 null
 */
function parseStateFile(path) {
    try {
        let file = Gio.File.new_for_path(path);
        let [success, contents] = file.load_contents(null);
        if (!success) {
            return null;
        }
        // imports.byteArray 已棄用，改用 TextDecoder
        let decoder = new TextDecoder("utf-8");
        let text = decoder.decode(contents);
        return JSON.parse(text);
    } catch (e) {
        log("Claude Usage: 讀取 state.json 失敗: " + e.message);
        return null;
    }
}

/**
 * 格式化 token 數字（千分位）
 * @param {number} tokens
 * @returns {string}
 */
function formatTokens(tokens) {
    if (tokens >= 1000000) {
        return (tokens / 1000000).toFixed(2) + "M";
    } else if (tokens >= 1000) {
        return (tokens / 1000).toFixed(1) + "K";
    }
    return tokens.toString();
}

/**
 * 根據 severity 取得進度條樣式類別
 * @param {string} severity
 * @param {number} percent
 * @returns {string}
 */
function getProgressClass(severity, percent) {
    if (severity === "critical" || percent >= 90) {
        return "claude-usage-progress-fill critical";
    } else if (severity === "warning" || percent >= 70) {
        return "claude-usage-progress-fill warning";
    }
    return "claude-usage-progress-fill";
}

/**
 * 建立單一 limit 的 UI 列
 * @param {Object} limit - limit 物件
 * @param {number} barWidth - 進度條總寬度（像素）
 * @returns {St.BoxLayout}
 */
function createLimitRow(limit, barWidth) {
    let row = new St.BoxLayout({
        style_class: "claude-usage-limit-row",
        vertical: true,
        x_expand: true,
    });

    // Header: label + percent
    let header = new St.BoxLayout({
        style_class: "claude-usage-limit-header",
        x_expand: true,
    });

    let label = new St.Label({
        style_class: "claude-usage-limit-label",
        text: limit.label || "未知限制",
        x_expand: true,
        x_align: St.Align.START,
    });

    let percentText = (limit.percent !== null && limit.percent !== undefined)
        ? limit.percent + "%"
        : "—";

    let percent = new St.Label({
        style_class: "claude-usage-limit-percent",
        text: percentText,
        x_align: St.Align.END,
    });

    header.add_child(label);
    header.add_child(percent);

    // Progress bar
    let barContainer = new St.BoxLayout({
        style_class: "claude-usage-progress-bar",
        x_expand: true,
    });

    let fillPercent = (limit.percent !== null && limit.percent !== undefined)
        ? Math.min(100, Math.max(0, limit.percent))
        : 0;

    // St 的 width 是像素數值（gfloat），不接受 "50%" 這種百分比字串——
    // 傳字串會變成 NaN，Clutter 直接拒絕繪製，整條進度條就消失了。
    // 所以這裡自己換算成像素。
    let totalWidth = Number(barWidth);
    if (!isFinite(totalWidth) || totalWidth <= 0) {
        totalWidth = DEFAULT_BAR_WIDTH;
    }
    let fillWidth = Math.round(totalWidth * fillPercent / 100);

    let fill = new St.BoxLayout({
        style_class: getProgressClass(limit.severity, fillPercent),
    });
    barContainer.set_width(totalWidth);
    fill.set_width(fillWidth);

    barContainer.add_child(fill);

    // Reset time
    let resetText = limit.resets_in_text
        ? "重置：" + limit.resets_in_text
        : (limit.resets_at ? "重置：" + limit.resets_at : "");

    let reset = new St.Label({
        style_class: "claude-usage-limit-reset",
        text: resetText,
        x_align: St.Align.START,
    });

    row.add_child(header);
    row.add_child(barContainer);
    row.add_child(reset);

    return row;
}

/**
 * 金額格式化：數字顯示 $xx.xx，null/undefined 顯示 —（B/C 區塊無資料時的佔位）
 * @param {number|null|undefined} v - 美金金額
 * @returns {string}
 */
function formatUsd(v) {
    return "$" + (v !== null && v !== undefined ? v.toFixed(2) : "—");
}

/**
 * 建立成本區塊
 * @param {Object} cost - cost 物件
 * @param {boolean} showCost - 是否顯示成本
 * @returns {St.BoxLayout|null}
 */
function createCostSection(cost, showCost) {
    if (!showCost) {
        return null;
    }

    let section = new St.BoxLayout({
        style_class: "claude-usage-section",
        vertical: true,
        x_expand: true,
    });

    let title = new St.Label({
        style_class: "claude-usage-section-title",
        text: "成本估算",
    });

    section.add_child(title);

    // 表頭：左欄空白，兩個金額欄放「今日」「本週」。
    // 金額欄寬度必須與明細列一致（70 像素），否則欄位對不齊。
    let headerRow = new St.BoxLayout({
        style_class: "claude-usage-cost-row",
        x_expand: true,
    });

    let headerSpacer = new St.Label({
        style_class: "claude-usage-cost-label",
        text: "",
        x_expand: true,
    });

    let headerToday = new St.Label({
        style_class: "claude-usage-cost-value",
        text: "今日",
        x_align: St.Align.END,
        width: 70,
    });

    let headerWeek = new St.Label({
        style_class: "claude-usage-cost-value",
        text: "本週",
        x_align: St.Align.END,
        width: 70,
    });

    headerRow.add_child(headerSpacer);
    headerRow.add_child(headerToday);
    headerRow.add_child(headerWeek);
    section.add_child(headerRow);

    // 分模型明細：短名（靠左撐開）｜今日金額｜本週金額（固定像素寬、靠右，
    // 金額欄寬度固定才不會因數字長度不同左右跳動；St 只吃像素，不可用百分比）
    let byModel = cost.by_model || [];
    for (let i = 0; i < byModel.length; i++) {
        let entry = byModel[i];

        let modelRow = new St.BoxLayout({
            style_class: "claude-usage-cost-row",
            x_expand: true,
        });

        let modelLabel = new St.Label({
            style_class: "claude-usage-cost-label",
            text: entry.label || entry.model || "",
            x_align: St.Align.START,
            x_expand: true,
        });

        let modelToday = new St.Label({
            style_class: "claude-usage-cost-value",
            text: formatUsd(entry.today_usd),
            x_align: St.Align.END,
            width: 70,
        });

        let modelWeek = new St.Label({
            style_class: "claude-usage-cost-value",
            text: formatUsd(entry.week_usd),
            x_align: St.Align.END,
            width: 70,
        });

        modelRow.add_child(modelLabel);
        modelRow.add_child(modelToday);
        modelRow.add_child(modelWeek);
        section.add_child(modelRow);
    }

    // 合計列：與明細同樣的三欄結構，總額放在最下面。
    // week_usd 為 null 時顯示 —（沿用 formatUsd）。
    let totalRow = new St.BoxLayout({
        style_class: "claude-usage-cost-row",
        x_expand: true,
    });

    let totalLabel = new St.Label({
        style_class: "claude-usage-cost-label",
        text: "合計",
        x_align: St.Align.START,
        x_expand: true,
    });

    let totalToday = new St.Label({
        style_class: "claude-usage-cost-value",
        text: formatUsd(cost.today_usd),
        x_align: St.Align.END,
        width: 70,
    });

    let totalWeek = new St.Label({
        style_class: "claude-usage-cost-value",
        text: formatUsd(cost.week_usd),
        x_align: St.Align.END,
        width: 70,
    });

    totalRow.add_child(totalLabel);
    totalRow.add_child(totalToday);
    totalRow.add_child(totalWeek);
    section.add_child(totalRow);

    let note = new St.Label({
        style_class: "claude-usage-cost-note",
        text: "參考估算值（Max 訂閱制不依此收費）",
        x_align: St.Align.START,
    });

    section.add_child(note);

    return section;
}

/**
 * 建立專案排行區塊
 * @param {Array} projects - 專案陣列
 * @param {boolean} showProjects - 是否顯示專案
 * @returns {St.BoxLayout|null}
 */
function createProjectsSection(projects, showProjects) {
    if (!showProjects || !projects || projects.length === 0) {
        return null;
    }

    let section = new St.BoxLayout({
        style_class: "claude-usage-section",
        vertical: true,
        x_expand: true,
    });

    let title = new St.Label({
        style_class: "claude-usage-section-title",
        text: "專案排行 (Top 5)",
    });

    section.add_child(title);

    for (let i = 0; i < projects.length; i++) {
        let proj = projects[i];
        let row = new St.BoxLayout({
            style_class: "claude-usage-project-row",
            x_expand: true,
        });

        let name = new St.Label({
            style_class: "claude-usage-project-name",
            text: proj.name || "未知專案",
            x_align: St.Align.START,
            x_expand: true,
        });

        let tokens = new St.Label({
            style_class: "claude-usage-project-tokens",
            text: formatTokens(proj.tokens || 0),
            x_align: St.Align.END,
        });

        let percent = new St.Label({
            style_class: "claude-usage-project-percent",
            text: (proj.percent !== null && proj.percent !== undefined)
                ? proj.percent.toFixed(1) + "%"
                : "—",
            x_align: St.Align.END,
        });

        row.add_child(name);
        row.add_child(tokens);
        row.add_child(percent);
        section.add_child(row);
    }

    return section;
}

/**
 * 建立 Session Context 區塊（SPEC §10）
 * 結構完全比照 createProjectsSection：同樣的 null 早退、
 * 同樣的 St.BoxLayout + St.Label、同樣的三欄（專案短名 / token 數 / 百分比）。
 * @param {Array} sessions - session 陣列
 * @param {boolean} showSessions - 是否顯示 session
 * @returns {St.BoxLayout|null}
 */
function createSessionsSection(sessions, showSessions) {
    if (!showSessions || !sessions || sessions.length === 0) {
        return null;
    }

    let section = new St.BoxLayout({
        style_class: "claude-usage-section",
        vertical: true,
        x_expand: true,
    });

    let title = new St.Label({
        style_class: "claude-usage-section-title",
        text: "Session Context",
    });

    section.add_child(title);

    for (let i = 0; i < sessions.length; i++) {
        let session = sessions[i];
        let row = new St.BoxLayout({
            style_class: "claude-usage-session-row",
            x_expand: true,
        });

        let name = new St.Label({
            style_class: "claude-usage-session-name",
            text: session.project || "未知專案",
            x_align: St.Align.START,
            x_expand: true,
        });

        let tokens = new St.Label({
            style_class: "claude-usage-session-tokens",
            text: formatTokens(session.tokens || 0),
            x_align: St.Align.END,
        });

        // context_window / percent 為 null 時只顯示 token 數，百分比欄顯示 —。
        // 絕對不准在這裡補分母（例如沒有就當 200000），理由見 SPEC §10.1。
        let percent = new St.Label({
            style_class: "claude-usage-session-percent",
            text: (session.percent !== null && session.percent !== undefined)
                ? session.percent.toFixed(1) + "%"
                : "—",
            x_align: St.Align.END,
        });

        row.add_child(name);
        row.add_child(tokens);
        row.add_child(percent);
        section.add_child(row);
    }

    return section;
}

/**
 * 建立錯誤訊息區塊
 * @param {Array} errors - 錯誤訊息陣列
 * @returns {St.BoxLayout|null}
 */
function createErrorSection(errors) {
    if (!errors || errors.length === 0) {
        return null;
    }

    let section = new St.BoxLayout({
        style_class: "claude-usage-section",
        vertical: true,
        x_expand: true,
    });

    for (let i = 0; i < errors.length; i++) {
        let errorLabel = new St.Label({
            style_class: "claude-usage-error",
            text: errors[i],
            x_align: St.Align.START,
            x_expand: true,
        });
        errorLabel.clutter_text.line_wrap = true;
        errorLabel.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        section.add_child(errorLabel);
    }

    return section;
}

/**
 * 建立「尚無資料」提示
 * @returns {St.Label}
 */
function createNoDataLabel() {
    let label = new St.Label({
        style_class: "claude-usage-no-data",
        text: "尚未取得資料\n請確認 collector 已執行",
        x_align: St.Align.MIDDLE,
        x_expand: true,
        y_align: St.Align.MIDDLE,
        y_expand: true,
    });
    label.clutter_text.line_wrap = true;
    label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
    return label;
}

/**
 * 更新時間標籤
 * @param {St.Label} label
 * @param {string} generatedAt
 */
function updateTimestampLabel(label, generatedAt) {
    if (generatedAt) {
        label.set_text("更新：" + generatedAt);
    } else {
        label.set_text("");
    }
}

/**
 * Desklet 建構函式
 */
function ClaudeUsageDesklet(metadata, desklet_id) {
    this._init(metadata, desklet_id);
}

ClaudeUsageDesklet.prototype = {
    __proto__: Desklet.Desklet.prototype,

    _init: function(metadata, desklet_id) {
        Desklet.Desklet.prototype._init.call(this, metadata, desklet_id);

        // 設定管理
        this.settings = new Settings.DeskletSettings(
            this,
            metadata.uuid,
            desklet_id
        );

        this.settings.bindProperty(
            Settings.BindingDirection.IN,
            "update-interval",
            "updateInterval",
            this._onSettingsChanged.bind(this)
        );
        this.settings.bindProperty(
            Settings.BindingDirection.IN,
            "show-cost",
            "showCost",
            this._onSettingsChanged.bind(this)
        );
        this.settings.bindProperty(
            Settings.BindingDirection.IN,
            "show-projects",
            "showProjects",
            this._onSettingsChanged.bind(this)
        );
        this.settings.bindProperty(
            Settings.BindingDirection.IN,
            "show-sessions",
            "showSessions",
            this._onSettingsChanged.bind(this)
        );
        this.settings.bindProperty(
            Settings.BindingDirection.IN,
            "width",
            "deskletWidth",
            this._onSettingsChanged.bind(this)
        );

        // 狀態
        this.statePath = GLib.build_filenamev([
            GLib.get_home_dir(),
            ".cache",
            "claude-usage-widget",
            "state.json"
        ]);
        // Collector 執行路徑（字串，非陣列）
        this.collectorPython = GLib.build_filenamev([
            GLib.get_home_dir(),
            "Claude",
            "linux_claude_usage",
            ".venv",
            "bin",
            "python"
        ]);
        this.collectorScript = GLib.build_filenamev([
            GLib.get_home_dir(),
            "Claude",
            "linux_claude_usage",
            "collector",
            "main.py"
        ]);
        this.projectRoot = GLib.build_filenamev([
            GLib.get_home_dir(),
            "Claude",
            "linux_claude_usage"
        ]);

        // UI 建構
        this._buildUI();

        // 啟動定時器
        this._startUpdateTimer();

        // 立即更新一次
        this._updateData();
    },

    _buildUI: function() {
        // 主容器 - 防護 NaN
        let w = Number(this.deskletWidth);
        if (!isFinite(w) || w <= 0) w = DEFAULT_DESKLET_WIDTH;
        this.mainContainer = new St.BoxLayout({
            style_class: "claude-usage-root",
            vertical: true,
            width: w,
        });

        // Limits 區塊
        this.limitsSection = new St.BoxLayout({
            style_class: "claude-usage-section",
            vertical: true,
            x_expand: true,
        });

        this.limitsTitle = new St.Label({
            style_class: "claude-usage-section-title",
            text: "額度使用",
        });
        this.limitsSection.add_child(this.limitsTitle);

        this.limitsContainer = new St.BoxLayout({
            vertical: true,
            x_expand: true,
            style_class: "claude-usage-limits-container",
        });
        this.limitsSection.add_child(this.limitsContainer);

        // Cost 區塊
        this.costContainer = new St.BoxLayout({
            vertical: true,
            x_expand: true,
        });

        // Projects 區塊
        this.projectsContainer = new St.BoxLayout({
            vertical: true,
            x_expand: true,
        });

        // Sessions 區塊（D 區塊，掛在 projects 後面、error 前面）
        this.sessionsContainer = new St.BoxLayout({
            vertical: true,
            x_expand: true,
        });

        // Error 區塊
        this.errorContainer = new St.BoxLayout({
            vertical: true,
            x_expand: true,
        });

        // Updated timestamp
        this.timestampLabel = new St.Label({
            style_class: "claude-usage-updated",
            text: "",
            x_align: St.Align.END,
            x_expand: true,
        });

        // 組裝
        this.mainContainer.add_child(this.limitsSection);
        this.mainContainer.add_child(this.costContainer);
        this.mainContainer.add_child(this.projectsContainer);
        this.mainContainer.add_child(this.sessionsContainer);
        this.mainContainer.add_child(this.errorContainer);
        this.mainContainer.add_child(this.timestampLabel);

        this.setContent(this.mainContainer);

        // 左鍵開歷史週報（collector 產生的 report.html）；
        // 其他鍵放行給 Cinnamon（右鍵選單等）
        this.mainContainer.reactive = true;
        this.mainContainer.connect("button-press-event", (actor, event) => {
            if (event.get_button() !== 1) {
                return Clutter.EVENT_PROPAGATE;
            }
            this._openWeeklyReport();
            return Clutter.EVENT_STOP;
        });
    },

    /**
     * 進度條可用寬度（像素）。設定值可能是 undefined 或 NaN，一律退回預設值。
     */
    _barWidth: function() {
        let w = Number(this.deskletWidth);
        if (!isFinite(w) || w <= 0) {
            w = DEFAULT_DESKLET_WIDTH;
        }
        return Math.max(40, w - ROOT_HORIZONTAL_PADDING);
    },

    _onSettingsChanged: function() {
        // 更新寬度 - 防護 NaN
        if (this.mainContainer) {
            let w = Number(this.deskletWidth);
            if (!isFinite(w) || w <= 0) w = DEFAULT_DESKLET_WIDTH;
            this.mainContainer.set_width(w);
        }
        // 重新啟動定時器（間隔可能變了）
        this._startUpdateTimer();
        // 立即更新
        this._updateData();
    },

    _startUpdateTimer: function() {
        if (this.updateTimer) {
            Mainloop.source_remove(this.updateTimer);
        }
        let intervalMs = Math.max(10, this.updateInterval) * 1000;
        this.updateTimer = Mainloop.timeout_add(intervalMs, () => {
            this._updateData();
            return GLib.SOURCE_CONTINUE;
        });
    },

    /**
     * 非同步呼叫 collector 更新資料
     */
    _triggerCollector: function() {
        // 使用 -m collector.main 方式，從專案根目錄執行
        let args = [
            this.collectorPython,
            "-m",
            "collector.main"
        ];

        GLib.spawn_async(
            this.projectRoot,  // working directory
            args,              // argv
            null,              // envp
            GLib.SpawnFlags.SEARCH_PATH,  // 不用 DO_NOT_REAP_CHILD，避免殭屍行程
            null               // child_setup
        );
    },

    /**
     * 用瀏覽器開啟歷史週報
     */
    _openWeeklyReport: function() {
        try {
            let reportPath = GLib.build_filenamev([
                GLib.get_home_dir(),
                ".cache",
                "claude-usage-widget",
                "report.html"
            ]);
            GLib.spawn_async(
                null,
                ["xdg-open", reportPath],
                null,
                GLib.SpawnFlags.SEARCH_PATH,
                null
            );
        } catch (e) {
            log("Claude Usage: 開啟週報失敗: " + e.message);
        }
    },

    /**
     * 讀取 state.json 並更新 UI
     */
    _updateData: function() {
        // 觸發 collector 背景更新
        this._triggerCollector();

        // 讀取 state.json
        let state = parseStateFile(this.statePath);

        if (!state) {
            this._showNoData();
            return;
        }

        // 檢查必要欄位
        let requiredKeys = ["schema_version", "generated_at", "ok", "errors",
                           "limits", "cost", "projects", "totals"];
        let hasAllKeys = requiredKeys.every(k => k in state);
        if (!hasAllKeys) {
            this._showNoData();
            return;
        }

        // 更新 Limits
        this._updateLimits(state.limits || []);

        // 更新 Cost
        this._updateCost(state.cost || {});

        // 更新 Projects
        this._updateProjects(state.projects || []);

        // 更新 Sessions（D 區塊）
        this._updateSessions(state.sessions || []);

        // 更新 Errors
        this._updateErrors(state.errors || []);

        // 更新時間戳
        updateTimestampLabel(this.timestampLabel, state.generated_at || "");
    },

    _showNoData: function() {
        // 清空所有區塊
        this._clearContainer(this.limitsContainer);
        this._clearContainer(this.costContainer);
        this._clearContainer(this.projectsContainer);
        this._clearContainer(this.sessionsContainer);
        this._clearContainer(this.errorContainer);

        // 顯示無資料訊息
        let noData = createNoDataLabel();
        this.limitsContainer.add_child(noData);
        updateTimestampLabel(this.timestampLabel, "");
    },

    _clearContainer: function(container) {
        if (container && container.get_children) {
            let children = container.get_children();
            for (let i = 0; i < children.length; i++) {
                container.remove_child(children[i]);
            }
        }
    },

    _updateLimits: function(limits) {
        this._clearContainer(this.limitsContainer);

        if (!limits || limits.length === 0) {
            let label = new St.Label({
                style_class: "claude-usage-no-data",
                text: "無額度資料",
                x_align: St.Align.START,
            });
            this.limitsContainer.add_child(label);
            return;
        }

        for (let i = 0; i < limits.length; i++) {
            let row = createLimitRow(limits[i], this._barWidth());
            this.limitsContainer.add_child(row);
        }
    },

    _updateCost: function(cost) {
        this._clearContainer(this.costContainer);

        let section = createCostSection(cost, this.showCost);
        if (section) {
            this.costContainer.add_child(section);
        }
    },

    _updateProjects: function(projects) {
        this._clearContainer(this.projectsContainer);

        let section = createProjectsSection(projects, this.showProjects);
        if (section) {
            this.projectsContainer.add_child(section);
        }
    },

    _updateSessions: function(sessions) {
        this._clearContainer(this.sessionsContainer);

        let section = createSessionsSection(sessions, this.showSessions);
        if (section) {
            this.sessionsContainer.add_child(section);
        }
    },

    _updateErrors: function(errors) {
        this._clearContainer(this.errorContainer);

        if (errors && errors.length > 0) {
            let section = createErrorSection(errors);
            if (section) {
                this.errorContainer.add_child(section);
            }
        }
    },

    on_desklet_removed: function() {
        if (this.updateTimer) {
            Mainloop.source_remove(this.updateTimer);
            this.updateTimer = null;
        }
    },
};

/**
 * Desklet 進入點
 */
function main(metadata, desklet_id) {
    return new ClaudeUsageDesklet(metadata, desklet_id);
}