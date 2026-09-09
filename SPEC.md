# SPEC — Cinnamon 桌面 Claude 用量 Widget

版本 0.1（2026-09-08 建立）。本檔是**唯一規格權威**；實作行為規則見 `AGENTS.md`，
專案脈絡見 `CLAUDE.md`。三份檔不重複同一件事。

---

## 1. 目標

在 Cinnamon 桌面上常駐一個 desklet，隨時看得到 Claude 用量，不必開瀏覽器或打指令。

必須同時呈現三類資訊（Frank 2026-09-08 拍板，三者皆為必要，非二選一）：

| # | 區塊 | 內容 | 資料來源 |
|---|---|---|---|
| A | **額度**（主視覺） | 5 小時 session %、每週 %、分模型週限 %，各自的重置時間 | Usage API |
| B | **成本估算** | 今日 / 本週的 token 換算美金 | 逐字稿 + 價格表 |
| C | **專案排行** | 各專案今日 token 佔比（Top 5） | 逐字稿 |

---

## 2. 資料來源（皆已實測，Evidence 層）

### 2.1 來源 A：Usage API（額度百分比）

```
GET https://api.anthropic.com/api/oauth/usage
Authorization: Bearer <claudeAiOauth.accessToken>
anthropic-beta: oauth-2025-04-20
```

Token 取自 `~/.claude/.credentials.json` 的 `claudeAiOauth.accessToken`。
2026-09-08 實測 HTTP 200，回傳與 claude.ai Settings→Usage 畫面一致。

**只讀 `limits` 陣列，不要硬編模型名。** 每個元素長這樣：

```json
{
  "kind": "session | weekly_all | weekly_scoped",
  "group": "session | weekly",
  "percent": 34,
  "severity": "normal",
  "resets_at": "2026-09-11T17:00:00+00:00",
  "scope": { "model": { "display_name": "Fable" }, "surface": null },
  "is_active": true
}
```

顯示規則：
- 標題文字＝`scope.model.display_name` 有值就用它，否則依 `kind` 對應
  （`session`→「本次 session」、`weekly_all`→「本週全部模型」）。
- **未來多出新的 limit 條目要能自動顯示**，不得因為程式沒認得就整條吞掉。
- `resets_at` 是 UTC，**顯示一律換算台灣時間（UTC+8）**，格式如「4 小時 46 分後重置」。

頂層還有 `five_hour` / `seven_day` 等欄位，與 `limits` 重複，**只當 `limits` 缺漏時的
fallback**，不平行維護兩套邏輯。

### 2.2 來源 B：逐字稿（token 實算）

`~/.claude/projects/<專案目錄名>/**/*.jsonl`，每行一個 JSON。
`type == "assistant"` 的行有 `message.model` 與 `message.usage`：

```json
{"input_tokens":2,"cache_creation_input_tokens":42032,
 "cache_read_input_tokens":29800,"output_tokens":489,
 "output_tokens_details":{"thinking_tokens":184}}
```

專案名＝目錄名反解（`-home-lintzuyang-Claude-quantum` → `quantum`）。
時間戳用該行的 `timestamp`（UTC），分日一律按**台灣時間**切。

⚠️ **不要用 `~/.claude/stats-cache.json`。** 它停在 2026-08-12 就沒再更新，是死資料。

---

## 3. 🔴 硬性效能約束

本機是 **2012 iMac (i5)**，且逐字稿現況 **335 檔 / 254 MB 且持續長大**。

- **禁止每次更新都全量掃描。** 必須做增量：記住每個檔的 `(size, mtime)`，
  只讀「變大的檔案從上次 offset 之後的部分」，其餘直接跳過。
- 快取放 `~/.cache/claude-usage-widget/`，不寫進專案目錄、不寫進 `~/.claude`。
- **絕不在 desklet 的 GJS 主執行緒解析 jsonl** —— 會直接凍住整個桌面。

---

## 4. 架構：兩層，用一個 JSON 檔接起來

```
collector/（Python 3，背景執行）
   └─ 讀 API + 增量掃逐字稿 → 寫 state.json
                    ↓
desklet/（cjs/GJS，只讀 state.json 畫圖）
```

分層理由：GJS 適合畫 UI、不適合啃 254MB 檔案；Python 適合啃檔案、不該碰 UI。
接縫只有一個檔，兩邊可獨立測試與替換。

### 4.1 介面契約（**先定死，實作不得更動**）

collector 輸出 `~/.cache/claude-usage-widget/state.json`，schema：

```json
{
  "schema_version": 1,
  "generated_at": "2026-09-08T21:55:00+08:00",
  "ok": true,
  "errors": [],

  "limits": [
    { "label": "本次 session", "percent": 3, "severity": "normal",
      "resets_at": "2026-09-09T02:40:00+08:00", "resets_in_text": "4 小時 46 分後" }
  ],

  "cost": {
    "today_usd": 12.34,
    "week_usd": 78.90,
    "pricing_version": "2026-09-08",
    "by_model": [
      { "model": "claude-opus-5", "label": "Opus 5",
        "today_usd": 8.20, "week_usd": 51.30 }
    ]
  },

  "projects": [
    { "name": "quantum", "tokens": 1234567, "percent": 42.1 }
  ],

  "totals": {
    "today_tokens": 2934567,
    "today_by_model": {
      "claude-opus-5": { "input_tokens": 120, "output_tokens": 45000,
                         "cache_creation_input_tokens": 900000,
                         "cache_read_input_tokens": 1954880 }
    }
  }
}
```

規則：
- **`today_by_model` 的值是「分項 usage dict」不是單一數字**——成本估算要分別套用
  input / output / cache 寫入 / cache 讀取四種單價，只有總數算不出金額。
- **所有時間欄位一律已經是台灣時間（UTC+8）的 ISO 字串**，desklet 不做時區換算。
- **`ok: false` 時其餘欄位仍須存在**（可為空陣列 / null），desklet 不得因缺欄位而炸掉。
- `errors` 是人看得懂的中文字串陣列，會直接顯示在 widget 上。

### 4.2 失敗必須是「可讀的降級」，不是空白

| 狀況 | 行為 |
|---|---|
| API 401 / token 過期 | `ok:false`、errors 加「登入已過期，請在終端機執行一次 claude」。**B、C 區塊照常顯示**（不依賴 API） |
| 沒有網路 | 同上，額度區塊顯示上次成功的數值並標「(舊資料)」 |
| `.credentials.json` 讀不到 | 同上，訊息換成「找不到憑證檔」 |
| 逐字稿目錄不存在 | 額度照常，B/C 顯示「—」 |

---

## 5. 🔒 安全約束（不可協商）

1. **憑證只讀，絕不寫入。** collector **不得**嘗試 refresh token、不得改寫
   `~/.claude/.credentials.json`。token 過期就降級顯示，由 Frank 自己跑一次 `claude` 續期。
   （理由：跟 CLI 搶著寫憑證檔會把登入狀態弄壞。）
2. **token 絕不出現在 log、stdout、state.json、錯誤訊息、commit 裡。** 任何一處出現即為缺陷。
3. `state.json` 權限設 `0600`。
4. **只打 `api.anthropic.com` 這一個網域**，不得有其他對外連線、不得有遙測。
5. `.gitignore` 必須擋掉快取與任何 `*.credentials*`。

---

## 6. 成本估算（B 區塊）

價格表獨立成 `collector/pricing.json`，欄位含 `input` / `output` /
`cache_write` / `cache_read` 的每百萬 token 單價與 `version` 日期。

- 程式**不得把價格寫死在 .py 裡**，一律讀 JSON。
- 找不到某個模型的價格 → 該模型不計入，並在 `errors` 加一條「模型 X 無價格資料」，
  **不得靜默當 0**（靜默當 0 會讓金額看起來很漂亮但是錯的）。
- ⚠️ 顯示時要標明這是**參考估算值**：Max 訂閱制下實際不會照這個金額收費。

### 6.1 分模型明細（`cost.by_model`）

只有總額看不出錢花在哪個模型上，所以總額之外還要給分項。

- 陣列，**依 `week_usd` 由大到小排序**，花最多的排最前面。
- **只列有用量的模型**（四個分項全為 0 的跳過，與 §6 的錯誤訊息規則一致）。
- 查不到價格的模型**不進這個陣列**（沒有金額可放），但 §6 的錯誤訊息照舊要有——
  使用者從錯誤訊息知道為什麼它沒出現，不是被無聲吞掉。
- `label` 是給人看的短名稱，**存在 `pricing.json` 每個模型的 `display` 欄位**，
  不在程式裡寫死對照表，也不從 model id 硬拼（`claude-fable-5-1` 拼不出「Fable 5.1」）。
- 🔴 **成本算法只能有一套。** 分項與總額必須來自同一個計算函式，
  不得為了分項另寫一份乘法——兩份遲早會對不起來，而且對不起來時沒人看得出是哪邊錯。

⚠️ **`pricing.json` 的 `version` 欄位在價格表內容變動時必須跟著更新**，
它會顯示在 widget 上；表變了版本沒變等於騙人。

---

## 7. 更新頻率

- API：每 **5 分鐘**。⚠️ **這不是禮貌問題，是硬限制**——2026-09-08 實際被
  Anthropic 回 `429 Too Many Requests`，額度區塊整塊變空白。
  原因是 desklet 每 30 秒更新一次、每次都叫 collector、collector 每次都打 API。
  ⇒ **節流必須做在 collector 裡**（`fetch_usage_throttled`），不能指望呼叫端自律：
  desklet 的更新間隔是使用者可調的，最短 10 秒。
  被回 429 之後改用 **15 分鐘** 的退避間隔，且**沿用上次成功的資料繼續顯示**
  （標明是舊資料），不要變成空白。相關契約由 tests 中「API 節流」那三條守著。
- 逐字稿增量掃描：每 **60 秒**。
- desklet 讀 `state.json`：每 **30 秒**，用 `GLib.spawn_async` 非同步呼叫 collector，
  **不得用同步呼叫**（會凍桌面）。

---

## 8. Desklet 規格

- 安裝路徑 `~/.local/share/cinnamon/desklets/claude-usage@lintzuyang/`
- 環境：Cinnamon **6.6.9**、直譯器 `cjs`（GJS）
- 必要檔：`metadata.json`、`desklet.js`、`settings-schema.json`、`stylesheet.css`
- 可設定項（settings-schema）：更新間隔、是否顯示成本、是否顯示專案排行、寬度
- UI：額度用橫條進度條（對應截圖那三條），成本與排行用文字列
- 深淺色主題都要能看（用 Cinnamon 主題色，不要寫死背景色）

---

## 9. 驗收標準

1. `python3 collector/main.py` 跑得完，產出的 `state.json` **完全符合 §4.1 schema**。
2. 測試全綠：`python3 -m pytest tests/ -v`。
3. 斷網 / 假造壞 token 兩種情境下，collector 仍 exit 0 且產出合法 `state.json`（`ok:false`）。
4. 第二次執行明顯比第一次快（證明增量掃描真的有生效，不是每次全掃）。
5. desklet 加到桌面後不當機，數字與 `claude.ai → Settings → Usage` 一致。
6. `grep -rE "sk-|accessToken|Bearer [A-Za-z0-9]" --include=*.py --include=*.js --include=*.json .`
   在 repo 內查無憑證值。
