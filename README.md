# Claude Usage Widget

[English](README.en.md)

在 Linux Cinnamon 桌面上常駐顯示 [Claude Code](https://claude.com/claude-code) 用量的 desklet（小工具）：額度使用率、成本估算、各專案 token 排行、每個對話當下的 context 佔用，外加可點開的歷史週報。不用開瀏覽器、不用打指令，桌面上隨時看得到。

![license](https://img.shields.io/badge/license-Apache%202.0-blue)

## 功能

- **額度使用率**：5 小時 session 限額、每週總限額、分模型週限額，各自有進度條與「幾小時後重置」倒數
- **成本估算**：今日 / 本週花費的美金估算，附分模型明細（僅供參考，Max 訂閱制不會照這個金額收費）
- **專案排行**：今日 token 用量 Top 5 專案
- **Session Context**：目前還在進行中的對話各自用掉多少 context，以及佔 context 視窗的比例（例 `13.5% of 1M`）。顯示最近 5 分鐘內有活動的對話，最多 3 個
- **歷史週報**：點一下 desklet 用瀏覽器開啟本機產生的 HTML 週報，按週彙總 token 與金額

## 環境需求

- Linux 桌面環境 **Cinnamon 6.6 以上**
- **Python 3.9 以上**（本機以 3.12 測試，執行期不需要任何第三方套件，只用標準庫）
- 已經登入過的 [Claude Code CLI](https://claude.com/claude-code)（讀取 `~/.claude/.credentials.json` 裡的 OAuth token 來查額度）

## 安裝

⚠️ desklet 內部把 collector 的路徑寫死在 `~/Claude/linux_claude_usage`，所以這個 repo **必須 clone 到這個固定路徑**才能正常運作。

```bash
mkdir -p ~/Claude
git clone https://github.com/franky5440-afk/linux_claude_usage.git ~/Claude/linux_claude_usage
cd ~/Claude/linux_claude_usage

# 建立 venv（執行期不需要裝任何套件，只是配合 desklet 寫死的路徑）
python3 -m venv .venv

# 想跑測試才需要裝這個
.venv/bin/pip install pytest

# 複製 desklet 檔案到 Cinnamon 的 desklets 目錄
./install.sh
```

安裝完之後：

1. 打開 Cinnamon 設定 → Desklets（小工具）
2. 在「可用的 Desklets」找到「Claude 用量監控」，新增到桌面
3. 讓 collector 有資料可顯示：手動跑一次，或加進 crontab 排程

   ```bash
   cd ~/Claude/linux_claude_usage && .venv/bin/python -m collector.main
   ```

   排程範例（每分鐘跑一次）：

   ```
   * * * * * cd ~/Claude/linux_claude_usage && .venv/bin/python -m collector.main
   ```

4. 右鍵點桌面上的 desklet → 設定，可調整更新間隔、要不要顯示成本 / 專案排行 / Session Context、寬度

## 架構

兩層架構，用一個 JSON 檔接起來：

- **collector**（Python）：定期打 Usage API 查額度百分比、增量掃描 `~/.claude/projects/**/*.jsonl` 逐字稿算 token 與成本，寫成 `~/.cache/claude-usage-widget/state.json`
- **desklet**（GJS / Cinnamon）：讀 `state.json` 畫出畫面，不直接碰任何憑證或逐字稿

完整規格見 [SPEC.md](SPEC.md)。

### 額度百分比 vs. Session Context 百分比

這兩個數字長得像，但**意義完全不同**，別搞混：

| | 額度使用率 | Session Context |
|---|---|---|
| 代表什麼 | 計費窗口用掉多少配額 | 這個對話目前佔掉多少 context 視窗 |
| 什麼時候歸零 | 時間到了自動重置 | 對話壓縮（compaction）後會掉下來 |
| 資料來源 | Usage API | 逐字稿裡最後一則回覆的 usage 欄位 |

Context 佔用＝最後一則回覆的 `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`。
分母從逐字稿裡的 model id 判斷（帶 `[1m]` 就是 1,000,000，否則 200,000）；
**判斷不出來時只顯示 token 數、百分比留白**，不會猜一個分母湊數。

## 安全性

- **憑證只讀不寫**：只讀 `~/.claude/.credentials.json` 的 `accessToken`，不會嘗試 refresh 或改寫它
- **token 絕不落地**：不寫進 log、`state.json`、錯誤訊息或任何檔案
- **只對外連一個網域**：`api.anthropic.com`，沒有任何遙測或其他連線
- `state.json` 寫入時權限固定為 `0600`

## 免責聲明

顯示的成本估算是**參考值**，不是官方帳單。實際計費以 Claude 帳戶的訂閱方案為準。

## 授權

[Apache License 2.0](LICENSE)
