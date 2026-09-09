"""介面契約測試——這是規格，不是建議。

⚠️ builder 不得修改本檔的斷言。測試錯了請回報，不要自己改綠。
每個測試上方的註解說明「為什麼」這條契約存在。
"""
import json
import os
import sys
import pytest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collector import main, pricing, transcript_scan, usage_api  # noqa: E402

TW = timezone(timedelta(hours=8))

# 一份假的 API 回應，欄位照 2026-09-08 實測結果，值改成好認的數字
FAKE_API = {
    "limits": [
        {"kind": "session", "group": "session", "percent": 3,
         "severity": "normal", "resets_at": "2026-09-08T18:40:00+00:00",
         "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 34,
         "severity": "normal", "resets_at": "2026-09-11T17:00:00+00:00",
         "scope": None, "is_active": True},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 4,
         "severity": "normal", "resets_at": "2026-09-11T17:00:00+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
         "is_active": False},
    ]
}


# --- 額度區塊 -------------------------------------------------------------

def test_limits_每一條都要出現在輸出裡():
    """API 回三條就要顯示三條。少一條代表使用者少看到一個會擋住他的限制。"""
    out = usage_api.normalize_limits(FAKE_API)
    assert len(out) == 3


def test_有_display_name_就用它當標題():
    """未來新增的模型限制要能自動長出來，不靠程式碼硬編模型名。"""
    out = usage_api.normalize_limits(FAKE_API)
    labels = [x["label"] for x in out]
    assert "Fable" in labels


def test_未知的_kind_不可以被吞掉():
    """API 是逆向挖出來的、隨時會變。認不得的條目要照顯示，不是消失。"""
    data = {"limits": [{"kind": "某個未來才有的限制", "group": "weekly",
                        "percent": 77, "severity": "normal",
                        "resets_at": "2026-09-11T17:00:00+00:00",
                        "scope": None, "is_active": True}]}
    out = usage_api.normalize_limits(data)
    assert len(out) == 1
    assert out[0]["percent"] == 77
    assert out[0]["label"], "認不得的 kind 也要給一個非空的標題"


def test_重置時間要換算成台灣時間():
    """SPEC §4.1：state.json 內所有時間已是 UTC+8，desklet 不做換算。"""
    out = usage_api.normalize_limits(FAKE_API)
    ts = datetime.fromisoformat(out[0]["resets_at"])
    assert ts.utcoffset() == timedelta(hours=8)
    # 18:40 UTC → 台灣 02:40（隔天）
    assert ts.hour == 2 and ts.minute == 40


# --- 成本估算 -------------------------------------------------------------

def test_沒有價格的模型不可以靜默當零():
    """靜默當 0 會讓金額看起來很漂亮但是錯的，比報錯還糟。"""
    table = {"version": "test", "models": {
        "claude-opus-5": {"input": 1.0, "output": 1.0,
                          "cache_write": 1.0, "cache_read": 1.0}}}
    usd, errors = pricing.estimate_cost(
        {"claude-opus-5": {"input_tokens": 1_000_000, "output_tokens": 0,
                           "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
         "某個沒登記價格的模型": {"input_tokens": 999_999_999, "output_tokens": 0,
                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
        table)
    assert abs(usd - 1.0) < 1e-6, "沒價格的模型不該被計入金額"
    assert any("某個沒登記價格的模型" in e for e in errors), "但一定要留下一條錯誤訊息"


# --- 增量掃描 -------------------------------------------------------------

def test_檔案沒變就不重讀(tmp_path):
    """SPEC §3：254MB 逐字稿每次全掃會拖垮 2012 iMac，這條是效能硬需求。"""
    proj = tmp_path / "projects" / "-home-lintzuyang-Claude-demo"
    proj.mkdir(parents=True)
    f = proj / "s1.jsonl"
    line = json.dumps({"type": "assistant",
                       "timestamp": datetime.now(timezone.utc).isoformat(),
                       "message": {"model": "claude-opus-5",
                                   "usage": {"input_tokens": 10, "output_tokens": 20,
                                             "cache_creation_input_tokens": 0,
                                             "cache_read_input_tokens": 0}}})
    f.write_text(line + "\n")
    cache = tmp_path / "cache"

    first = transcript_scan.scan(cache_dir=cache, projects_dir=tmp_path / "projects")
    second = transcript_scan.scan(cache_dir=cache, projects_dir=tmp_path / "projects")

    assert first["bytes_read"] > 0
    assert second["bytes_read"] == 0, "第二次不該重讀任何位元組"
    assert second["totals"]["today_tokens"] == first["totals"]["today_tokens"], \
        "跳過重讀不代表數字歸零，累計值要從快取還原"


def test_檔案長大只讀新增的部分(tmp_path):
    proj = tmp_path / "projects" / "-home-lintzuyang-Claude-demo"
    proj.mkdir(parents=True)
    f = proj / "s1.jsonl"
    now = datetime.now(timezone.utc).isoformat()

    def row(out_tokens):
        return json.dumps({"type": "assistant", "timestamp": now,
                           "message": {"model": "claude-opus-5",
                                       "usage": {"input_tokens": 0, "output_tokens": out_tokens,
                                                 "cache_creation_input_tokens": 0,
                                                 "cache_read_input_tokens": 0}}})

    f.write_text(row(100) + "\n")
    cache = tmp_path / "cache"
    transcript_scan.scan(cache_dir=cache, projects_dir=tmp_path / "projects")

    with open(f, "a") as fh:
        fh.write(row(50) + "\n")
    second = transcript_scan.scan(cache_dir=cache, projects_dir=tmp_path / "projects")

    assert 0 < second["bytes_read"] < len(row(100)) + len(row(50)), "只該讀新增那段"
    assert second["totals"]["today_tokens"] == 150, "累計要含先前已讀的部分"


def test_專案名要從目錄名反解(tmp_path):
    proj = tmp_path / "projects" / "-home-lintzuyang-Claude-quantum"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(json.dumps({
        "type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": {"model": "claude-opus-5",
                    "usage": {"input_tokens": 1, "output_tokens": 1,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}}}) + "\n")
    r = transcript_scan.scan(cache_dir=tmp_path / "cache",
                             projects_dir=tmp_path / "projects")
    assert [p["name"] for p in r["projects"]] == ["quantum"]


# --- state.json 契約 ------------------------------------------------------

REQUIRED_TOP = {"schema_version", "generated_at", "ok", "errors",
                "limits", "cost", "projects", "totals"}


def test_state_失敗時欄位仍須齊全():
    """desklet 不得因為缺欄位而炸掉——widget 壞掉比數字不準嚴重得多。"""
    state = main.build_state(api_result=None, api_error="登入已過期",
                             scan_result=None, scan_error="找不到逐字稿目錄")
    assert REQUIRED_TOP <= set(state)
    assert state["ok"] is False
    assert isinstance(state["limits"], list)
    assert isinstance(state["projects"], list)
    assert state["errors"], "失敗一定要留下人看得懂的訊息"


def test_state_可以被序列化成_json():
    state = main.build_state(api_result=None, api_error="x",
                             scan_result=None, scan_error="y")
    json.dumps(state)  # 不可拋例外


def test_api_掛掉時逐字稿區塊照常有值(tmp_path):
    """兩個資料源互相獨立，一邊斷了另一邊不該跟著消失。"""
    scan = {"totals": {"today_tokens": 42,
                       "today_by_model": {"claude-opus-5": {
                           "input_tokens": 42, "output_tokens": 0,
                           "cache_creation_input_tokens": 0,
                           "cache_read_input_tokens": 0}}},
            "projects": [{"name": "demo", "tokens": 42, "percent": 100.0}],
            "bytes_read": 0}
    state = main.build_state(api_result=None, api_error="沒有網路",
                             scan_result=scan, scan_error=None)
    assert state["totals"]["today_tokens"] == 42
    assert state["projects"][0]["name"] == "demo"


# --- 安全 -----------------------------------------------------------------

def test_state_內不得出現_token_字樣():
    state = main.build_state(api_result=FAKE_API, api_error=None,
                             scan_result=None, scan_error=None)
    blob = json.dumps(state)
    for bad in ("accessToken", "Bearer ", "refreshToken", "sk-ant"):
        assert bad not in blob, f"state.json 不該出現 {bad}"


# --- 專案目錄形式（2026-09-08 補：原契約只驗了 -Claude- 一種形式，
#     導致實作可以合法地把其他形式整個丟掉而測試全綠）------------------

def _write_row(path, out_tokens=100):
    from datetime import datetime as _dt
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "timestamp": _dt.now(timezone.utc).isoformat(),
            "message": {"model": "claude-opus-5",
                        "usage": {"input_tokens": 0, "output_tokens": out_tokens,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}}}) + "\n")


def test_不是_Claude_底下的專案也要算進去(tmp_path):
    """漏算的用量比算錯還危險——畫面顯示的數字會偏低但看起來完全正常。

    實際存在的三種目錄形式都要涵蓋：
      -home-<user>                          家目錄本身
      -home-<user>-Claude-<專案>            ~/Claude/ 底下
      -home-<user>-freebuff-project-<專案>  其他位置的專案
    """
    proj = tmp_path / "projects"
    _write_row(proj / "-home-someone" / "a.jsonl", 100)
    _write_row(proj / "-home-someone-Claude-demo" / "b.jsonl", 200)
    _write_row(proj / "-home-someone-freebuff-project-manga" / "c.jsonl", 300)

    r = transcript_scan.scan(cache_dir=tmp_path / "cache", projects_dir=proj)
    assert r["totals"]["today_tokens"] == 600, "三個專案都要算到，一個都不能漏"
    assert len(r["projects"]) == 3


def test_使用者名稱不可以寫死(tmp_path):
    """寫死使用者名稱的實作在別台機器上會靜默算出 0。"""
    proj = tmp_path / "projects"
    _write_row(proj / "-home-完全不同的使用者-Claude-demo" / "a.jsonl", 500)
    r = transcript_scan.scan(cache_dir=tmp_path / "cache", projects_dir=proj)
    assert r["totals"]["today_tokens"] == 500


def test_子目錄裡的逐字稿也要算(tmp_path):
    """subagent 的逐字稿放在 <session>/subagents/ 底下，不遞迴就整批漏掉。"""
    proj = tmp_path / "projects"
    _write_row(proj / "-home-someone-Claude-demo" / "s1.jsonl", 100)
    _write_row(proj / "-home-someone-Claude-demo" / "s1" / "subagents" / "a.jsonl", 700)
    r = transcript_scan.scan(cache_dir=tmp_path / "cache", projects_dir=proj)
    assert r["totals"]["today_tokens"] == 800, "子目錄的 jsonl 也要掃到"


def test_家目錄專案要有看得懂的名字(tmp_path):
    """排行榜上出現 -home-lintzuyang 這種原始目錄名對使用者沒有意義。"""
    proj = tmp_path / "projects"
    _write_row(proj / "-home-someone" / "a.jsonl", 100)
    r = transcript_scan.scan(cache_dir=tmp_path / "cache", projects_dir=proj)
    assert r["projects"][0]["name"] == "家目錄"


# --- API 節流（2026-09-08 補：實際被 Anthropic 回 429 Too Many Requests
#     因為 desklet 每 30 秒更新就打一次 API，違反 SPEC §7 的 5 分鐘規定）----

def test_節流窗口內不可以重複打_API(tmp_path, monkeypatch):
    """打太快會被回 429，整個額度區塊就變成空白——比更新慢一點嚴重得多。"""
    monkeypatch.setattr(main, "STATE_DIR", tmp_path)
    monkeypatch.setattr(main.usage_api, "read_access_token", lambda *a, **k: "fake-token")

    calls = []

    def fake_fetch(token, timeout=20):
        calls.append(token)
        return FAKE_API

    monkeypatch.setattr(main.usage_api, "fetch", fake_fetch)

    main.fetch_usage_throttled()
    main.fetch_usage_throttled()
    main.fetch_usage_throttled()

    assert len(calls) == 1, f"節流窗口內只該打一次 API，實際打了 {len(calls)} 次"


def test_被限流時要沿用上次資料而不是變空白(tmp_path, monkeypatch):
    """額度數字暫時舊掉，遠比整塊消失有用。"""
    monkeypatch.setattr(main, "STATE_DIR", tmp_path)
    monkeypatch.setattr(main.usage_api, "read_access_token", lambda *a, **k: "fake-token")
    monkeypatch.setattr(main.usage_api, "fetch", lambda *a, **k: FAKE_API)
    main.fetch_usage_throttled()  # 先取得一份好資料

    # 讓節流窗口過期，並讓下一次呼叫被限流
    cache = json.loads((tmp_path / "api_cache.json").read_text())
    cache["fetched_at"] = 0
    (tmp_path / "api_cache.json").write_text(json.dumps(cache))

    def boom(token, timeout=20):
        raise RuntimeError("HTTP 429: Too Many Requests")

    monkeypatch.setattr(main.usage_api, "fetch", boom)
    result, err = main.fetch_usage_throttled()

    assert result is not None and result.get("limits"), "被限流時仍要回傳上次的資料"
    assert err and "429" in err, "同時要留下一條看得懂的錯誤訊息"


def test_被限流後要拉長重試間隔(tmp_path, monkeypatch):
    """撞牆後還每 5 分鐘去撞一次，只會一直被擋。"""
    monkeypatch.setattr(main, "STATE_DIR", tmp_path)
    monkeypatch.setattr(main.usage_api, "read_access_token", lambda *a, **k: "fake-token")
    monkeypatch.setattr(main.usage_api, "fetch", lambda *a, **k: FAKE_API)
    main.fetch_usage_throttled()

    cache = json.loads((tmp_path / "api_cache.json").read_text())
    cache["fetched_at"] = 0
    (tmp_path / "api_cache.json").write_text(json.dumps(cache))
    monkeypatch.setattr(main.usage_api, "fetch",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 429")))
    main.fetch_usage_throttled()

    # 現在把時間往回撥到「超過一般節流窗口、但還沒到 backoff」
    cache = json.loads((tmp_path / "api_cache.json").read_text())
    assert cache["rate_limited"] is True
    cache["fetched_at"] = __import__("time").time() - (main.API_MIN_INTERVAL_SECONDS + 10)
    (tmp_path / "api_cache.json").write_text(json.dumps(cache))

    calls = []
    monkeypatch.setattr(main.usage_api, "fetch",
                        lambda *a, **k: (calls.append(1), FAKE_API)[1])
    main.fetch_usage_throttled()
    assert calls == [], "被限流後在 backoff 期間內不該再打 API"


# =====================================================================
# 以下為 2026-09-09 交接給下一個 session 的工作。
# 測試即規格：讓它們變綠即可，不要修改斷言。
# =====================================================================

# --- P0：跨日快取污染（實際發生過，數字差了 48 倍）--------------------

def test_跨日之後不可以把昨天的量當成今天(tmp_path):
    """2026-09-09 00:03 實測：走快取得到 107,872,128，清快取重算只有 2,224,443。

    根因是快取存了「今日累計」卻沒有記錄那是哪一天的，跨過午夜就整包沿用。
    這種錯特別危險——數字很大、看起來完全正常，不會有人察覺。

    契約：快取必須記錄它是哪一天算的；日期不是今天就必須重算，不可沿用。
    """
    proj = tmp_path / "projects"
    _write_row(proj / "-home-someone-Claude-demo" / "a.jsonl", 100)
    cache = tmp_path / "cache"

    first = transcript_scan.scan(cache_dir=cache, projects_dir=proj)
    assert first["totals"]["today_tokens"] == 100

    # 把快取偽裝成「昨天算的」，檔案本身不動
    stale_found = False
    for name in ("totals_cache.json", "scan_cache.json", "cache.json"):
        p = cache / name
        if not p.exists():
            continue
        data = json.loads(p.read_text())
        assert "scan_date" in data, (
            f"{name} 必須記錄 scan_date（今日累計是哪一天算的），"
            "否則無從判斷快取是不是隔夜的")
        yesterday = (datetime.now(TW) - timedelta(days=1)).date().isoformat()
        data["scan_date"] = yesterday
        p.write_text(json.dumps(data))
        stale_found = True
    assert stale_found, "找不到任何帶 scan_date 的快取檔"

    second = transcript_scan.scan(cache_dir=cache, projects_dir=proj)
    assert second["totals"]["today_tokens"] == 100, (
        "隔夜快取要作廢重算。這筆記錄的時間戳是今天，所以重算後仍是 100；"
        "若沿用隔夜快取會變成 200（昨天的 100 + 今天重讀的 100）")


# --- P1：本週成本（目前顯示 $— ）--------------------------------------

def test_scan_要一併回傳本週用量(tmp_path):
    """SPEC §1 的 B 區塊要「今日 / 本週」兩個數字，目前 week 只顯示 $—。"""
    proj = tmp_path / "projects"
    now = datetime.now(TW)
    d = proj / "-home-someone-Claude-demo"
    d.mkdir(parents=True)

    def row(dt, out_tokens):
        return json.dumps({
            "type": "assistant", "timestamp": dt.astimezone(timezone.utc).isoformat(),
            "message": {"model": "claude-opus-5",
                        "usage": {"input_tokens": 0, "output_tokens": out_tokens,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}}}) + "\n"

    with open(d / "a.jsonl", "w") as fh:
        fh.write(row(now, 100))                          # 今天
        fh.write(row(now - timedelta(days=30), 400))     # 三十天前 → 不算

    r = transcript_scan.scan(cache_dir=tmp_path / "cache", projects_dir=proj)
    assert r["totals"]["today_tokens"] == 100
    assert "week_by_model" in r["totals"], "要新增 week_by_model 欄位"
    week = r["totals"]["week_by_model"]["claude-opus-5"]
    assert week["output_tokens"] == 100, "三十天前那筆不算本週"


def test_widget的本週也要按週一切週不是最近七天(tmp_path):
    """widget 的「本週」與歷史週報必須是同一套定義，否則兩個畫面對不起來。

    Frank 2026-09-09 拍板：一律週一到週日。上週日即使落在最近七天內也不算本週。
    """
    proj = tmp_path / "projects"
    now = datetime.now(TW)
    monday = now - timedelta(days=now.weekday())   # 本週一（不論今天星期幾）
    last_sunday = monday - timedelta(days=1)       # 上週日：在七天內，但屬上一週
    d = proj / "-home-someone-Claude-demo"
    d.mkdir(parents=True)

    def row(dt, out_tokens):
        return json.dumps({
            "type": "assistant", "timestamp": dt.astimezone(timezone.utc).isoformat(),
            "message": {"model": "claude-opus-5",
                        "usage": {"input_tokens": 0, "output_tokens": out_tokens,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}}}) + "\n"

    with open(d / "a.jsonl", "w") as fh:
        fh.write(row(monday, 200))
        fh.write(row(last_sunday, 400))

    r = transcript_scan.scan(cache_dir=tmp_path / "cache", projects_dir=proj)
    week = r["totals"]["week_by_model"]["claude-opus-5"]
    assert week["output_tokens"] == 200, \
        "本週＝本週一起算；上週日那 400 在七天內但不屬本週"


def test_state_的本週成本要有數字(tmp_path):
    """目前寫死 None 並附一條「尚未實作」的錯誤訊息，補完後要換成真的金額。"""
    scan = {
        "totals": {
            "today_tokens": 100,
            "today_by_model": {"claude-opus-5": {
                "input_tokens": 1_000_000, "output_tokens": 0,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
            "week_by_model": {"claude-opus-5": {
                "input_tokens": 3_000_000, "output_tokens": 0,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
        },
        "projects": [], "bytes_read": 0,
    }
    state = main.build_state(api_result=None, api_error=None,
                             scan_result=scan, scan_error=None)
    assert state["cost"]["week_usd"] is not None, "本週成本不可再是 None"
    assert state["cost"]["week_usd"] > state["cost"]["today_usd"], \
        "本週包含今天，所以一定大於等於今日；本例中三倍用量應明顯較大"
    assert not any("尚未實作" in e for e in state["errors"]), \
        "實作完成後要移除那條「尚未實作」的錯誤訊息"


# --- 價格表涵蓋率 ---------------------------------------------------------

def test_fable_的價格四個欄位都要有而且_cache_read_是特例():
    """Fable 佔本機用量約兩成，缺價格會讓本週成本低估。

    🔴 Fable 5.1 / Mythos 5.1 的 cache 讀取是 input 的 0.025 倍，
    不是其他模型的 0.1 倍（官方定價頁的註腳）。套錯倍率會貴 4 倍，
    而本機 Fable 的用量有 97.7% 是 cache 讀取，等於整筆金額都靠這個數字。
    """
    table = pricing.load_table()
    fable = table["models"]["claude-fable-5-1"]
    assert fable["input"] == 10.0
    assert fable["output"] == 50.0
    assert fable["cache_write"] == 12.50, "5 分鐘 cache 寫入＝input × 1.25"
    assert fable["cache_read"] == 0.25, \
        "Fable 是 0.025 倍的特例（$0.25），不是一般的 0.1 倍（$1.00）"


def test_用量為零的模型不要報價格錯誤():
    """<synthetic> 這種佔位模型用量是 0，卻照樣佔一行錯誤訊息顯示在桌面上。

    算不出金額才需要警告；根本沒用量的東西沒有金額可算，不是錯誤。
    """
    by_model = {
        "<synthetic>": {"input_tokens": 0, "output_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0},
        "某個真的有用量但沒價格的模型": {
            "input_tokens": 500, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }
    _, errors = pricing.estimate_cost(by_model, pricing.load_table())
    assert not any("<synthetic>" in e for e in errors), \
        "用量為 0 的模型不該產生錯誤訊息"
    assert any("某個真的有用量但沒價格的模型" in e for e in errors), \
        "真的有用量卻沒價格仍必須報錯——不可趁機把這條防線一起關掉"


# --- 分模型成本明細（SPEC §6.1）------------------------------------------

def test_每個模型都要有給人看的短名稱():
    """widget 上不能顯示 claude-fable-5-1 這種字串，但短名也不能從 id 硬拼——
    `claude-fable-5-1` 拼不出「Fable 5.1」。⇒ 存在價格表裡。
    """
    table = pricing.load_table()
    for model_name, entry in table["models"].items():
        assert entry.get("display"), f"{model_name} 缺少 display 短名稱"
    assert table["models"]["claude-fable-5-1"]["display"] == "Fable 5.1"


def test_分項成本加起來要等於總額():
    """🔴 這是本節最重要的一條：成本算法只能有一套。

    如果為了做分項另寫一份乘法，兩份遲早會漂開，而且漂開時
    畫面上「總額」和「明細相加」對不起來，沒人看得出是哪邊錯。
    """
    by_model = {
        "claude-opus-5": {"input_tokens": 1_000_000, "output_tokens": 500_000,
                          "cache_creation_input_tokens": 200_000,
                          "cache_read_input_tokens": 9_000_000},
        "claude-fable-5-1": {"input_tokens": 100_000, "output_tokens": 50_000,
                             "cache_creation_input_tokens": 10_000,
                             "cache_read_input_tokens": 8_000_000},
    }
    table = pricing.load_table()
    per_model, _ = pricing.estimate_cost_by_model(by_model, table)
    total, _ = pricing.estimate_cost(by_model, table)
    assert sum(per_model.values()) == pytest.approx(total)


def test_state_的分項明細依本週金額由大到小排序():
    """花最多的要排最前面，不然使用者得自己掃一遍才知道錢花在哪。"""
    scan = {
        "totals": {
            "today_tokens": 100,
            "today_by_model": {
                "claude-haiku-4-5": {"input_tokens": 1_000_000, "output_tokens": 0,
                                     "cache_creation_input_tokens": 0,
                                     "cache_read_input_tokens": 0},
                "claude-opus-5": {"input_tokens": 1_000_000, "output_tokens": 0,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}},
            "week_by_model": {
                "claude-haiku-4-5": {"input_tokens": 1_000_000, "output_tokens": 0,
                                     "cache_creation_input_tokens": 0,
                                     "cache_read_input_tokens": 0},
                "claude-opus-5": {"input_tokens": 1_000_000, "output_tokens": 0,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}},
        },
        "projects": [], "bytes_read": 0,
    }
    state = main.build_state(api_result=None, api_error=None,
                            scan_result=scan, scan_error=None)
    rows = state["cost"]["by_model"]
    assert [r["model"] for r in rows] == ["claude-opus-5", "claude-haiku-4-5"], \
        "opus 每百萬 $5、haiku $1，opus 要排前面"
    assert rows[0]["label"] == "Opus 5"
    assert rows[0]["today_usd"] == pytest.approx(5.0)
    assert rows[0]["week_usd"] == pytest.approx(5.0)


def test_沒用量或沒價格的模型不進明細但錯誤訊息照舊():
    """明細裡放不了沒有金額的東西，但不可以因此把 §6 的警告一起吞掉——
    使用者要能從錯誤訊息知道「為什麼那個模型沒出現」。
    """
    zero = {"input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    scan = {
        "totals": {
            "today_tokens": 100,
            "today_by_model": {"<synthetic>": dict(zero),
                               "某個沒價格的新模型": {**zero, "output_tokens": 999}},
            "week_by_model": {"<synthetic>": dict(zero),
                              "某個沒價格的新模型": {**zero, "output_tokens": 999}},
        },
        "projects": [], "bytes_read": 0,
    }
    state = main.build_state(api_result=None, api_error=None,
                            scan_result=scan, scan_error=None)
    models = [r["model"] for r in state["cost"]["by_model"]]
    assert "<synthetic>" not in models and "某個沒價格的新模型" not in models
    assert any("某個沒價格的新模型" in e for e in state["errors"]), \
        "有用量卻沒價格仍必須報錯"
    assert not any("<synthetic>" in e for e in state["errors"])


# --- 歷史週報 -------------------------------------------------------------
#
# 背景：現有掃描是增量的（只讀檔案新增的部分），掃過的資料就丟了，
# 程式手上永遠只有「今天」。要看歷史，資料必須先被留下來。
# SPEC §3 禁止每次更新都全量掃描，所以不能「要看報表就重掃 254MB」。
#
# 解法：跨日作廢今日累計的時候（P0 那條），丟掉之前先把它寫進一本日結帳本。
# 一天一筆，一年 365 筆，讀寫都不痛，等於搭 P0 的便車拿到歷史。

def test_日結帳本存得進去也讀得回來(tmp_path):
    """帳本是歷史週報唯一的資料來源，存讀不對後面全錯。"""
    from collector import history
    cache = tmp_path / "cache"
    history.record_day(cache, "2026-09-01", {
        "claude-opus-5": {"input_tokens": 10, "output_tokens": 20,
                          "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0}}, project_tokens={"demo": 30})

    days = history.load(cache)["days"]
    assert "2026-09-01" in days
    assert days["2026-09-01"]["by_model"]["claude-opus-5"]["output_tokens"] == 20
    assert days["2026-09-01"]["projects"]["demo"] == 30


def test_同一天記兩次不可以變兩倍(tmp_path):
    """collector 每 60 秒跑一次，跨日那一刻前後可能連續記到同一天好幾次。

    帳本必須是「覆蓋」不是「累加」，否則歷史數字會隨執行次數膨脹——
    又是一種數字很大、看起來很正常的錯。
    """
    from collector import history
    cache = tmp_path / "cache"
    usage = {"claude-opus-5": {"input_tokens": 0, "output_tokens": 100,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0}}
    history.record_day(cache, "2026-09-01", usage, project_tokens={"demo": 100})
    history.record_day(cache, "2026-09-01", usage, project_tokens={"demo": 100})

    days = history.load(cache)["days"]
    assert days["2026-09-01"]["by_model"]["claude-opus-5"]["output_tokens"] == 100, \
        "同一天重複記錄要覆蓋，不可累加"


def test_跨日的時候要自動把昨天存進帳本(tmp_path):
    """這是整條功能的關鍵接縫：P0 作廢隔夜快取時，丟掉前必須先歸檔。

    漏掉這一步，帳本永遠是空的，週報永遠沒東西可看。
    """
    from collector import history
    proj = tmp_path / "projects"
    _write_row(proj / "-home-someone-Claude-demo" / "a.jsonl", 100)
    cache = tmp_path / "cache"

    transcript_scan.scan(cache_dir=cache, projects_dir=proj)

    # 把快取偽裝成昨天算的（同 P0 的手法）
    yesterday = (datetime.now(TW) - timedelta(days=1)).date().isoformat()
    for name in ("totals_cache.json", "scan_cache.json", "cache.json"):
        p = cache / name
        if p.exists():
            data = json.loads(p.read_text())
            data["scan_date"] = yesterday
            p.write_text(json.dumps(data))

    transcript_scan.scan(cache_dir=cache, projects_dir=proj)

    days = history.load(cache)["days"]
    assert yesterday in days, "隔夜快取作廢前必須先歸檔到帳本，不可直接丟掉"
    assert days[yesterday]["by_model"]["claude-opus-5"]["output_tokens"] == 100


def test_週報按台灣時間的週一切週(tmp_path):
    """人講「這週」是指週一到週日，不是「最近七天」。

    2026-09-01 是週二、09-07 是下週一 → 必須落在不同週。
    """
    from collector import history
    cache = tmp_path / "cache"
    for date_str, out in (("2026-09-01", 100),   # 週二
                          ("2026-09-03", 200),   # 週四（同一週）
                          ("2026-09-07", 400)):  # 下週一
        history.record_day(cache, date_str, {
            "claude-opus-5": {"input_tokens": 0, "output_tokens": out,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}},
            project_tokens={"demo": out})

    report = history.weekly_report(cache)
    weeks = {w["week_start"]: w for w in report}

    assert "2026-08-31" in weeks, "09-01 是週二，該週的週一是 08-31"
    assert "2026-09-07" in weeks, "09-07 本身是週一，自成一週"
    assert weeks["2026-08-31"]["tokens"] == 300, "同一週的 100+200 要合併"
    assert weeks["2026-09-07"]["tokens"] == 400


def test_週報要有金額而且走既有價格表(tmp_path):
    """成本邏輯只能有一套。另寫一套算法遲早跟今日成本對不起來。"""
    from collector import history
    cache = tmp_path / "cache"
    by_model = {"claude-opus-5": {"input_tokens": 1_000_000, "output_tokens": 0,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}}
    history.record_day(cache, "2026-09-01", by_model, project_tokens={"demo": 1_000_000})

    week = history.weekly_report(cache)[0]
    expected, _ = pricing.estimate_cost(by_model, pricing.load_table())
    assert week["usd"] == pytest.approx(expected), \
        "金額必須用 pricing.estimate_cost 算，不可自己另寫一套"


def test_可以一次性全量補建歷史(tmp_path):
    """帳本從安裝當天才開始長，等一個月才看得到第二週，功能等於廢的。

    ⇒ 要有一支一次性的全量掃描把過去補回來。
    它一輩子只跑一次，不違反 SPEC §3（禁止的是「每次更新都全掃」）。
    """
    from collector import history
    proj = tmp_path / "projects"
    d = proj / "-home-someone-Claude-demo"
    d.mkdir(parents=True)
    old = datetime.now(TW) - timedelta(days=10)
    with open(d / "a.jsonl", "w") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "timestamp": old.astimezone(timezone.utc).isoformat(),
            "message": {"model": "claude-opus-5",
                        "usage": {"input_tokens": 0, "output_tokens": 700,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}}}) + "\n")

    cache = tmp_path / "cache"
    history.rebuild(cache_dir=cache, projects_dir=proj)

    days = history.load(cache)["days"]
    old_date = old.date().isoformat()
    assert old_date in days, "全量補建要能撈到十天前的資料"
    assert days[old_date]["by_model"]["claude-opus-5"]["output_tokens"] == 700


def test_報表_html_是自足的而且不含憑證(tmp_path):
    """desklet 點一下用 xdg-open 開這份檔，所以它必須自己能看。

    另外它會被寫到磁碟上，SPEC §5.2 的「token 絕不出現在任何輸出」同樣適用。
    """
    from collector import history
    cache = tmp_path / "cache"
    history.record_day(cache, "2026-09-01", {
        "claude-opus-5": {"input_tokens": 0, "output_tokens": 100,
                          "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0}},
        project_tokens={"demo": 100})

    html = history.render_html(history.weekly_report(cache))
    assert "<html" in html.lower()
    assert "2026-09-01" in html or "2026-08-31" in html, "報表要看得到日期"
    assert "http://" not in html and "https://" not in html, \
        "報表必須自足，不可外連 CDN——離線也要看得到，且不得有對外連線（SPEC §5.4）"
    assert "Bearer" not in html and "accessToken" not in html


def test_合計要等於各列四捨五入後相加():
    """畫面上的合計必須自己加得起來。

    Frank 2026-09-09 拍板：合計改成「各列四捨五入後相加」，不是「原始值加總再四捨五入」。
    數學上後者比較準，但使用者拿計算機加一加對不上就不會信任這個 widget，
    而它的唯一工作就是顯示數字。誤差最多一兩分，畫面已註明是參考估算值。

    這組數字：haiku 與 opus 各 1.004（各自進位成 1.00），
    原始合計 2.008 會進位成 2.01，但各列相加是 2.00。
    """
    usage = {
        "claude-haiku-4-5": {"input_tokens": 1_004_000, "output_tokens": 0,
                             "cache_creation_input_tokens": 0,
                             "cache_read_input_tokens": 0},
        "claude-opus-5": {"input_tokens": 200_800, "output_tokens": 0,
                          "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0},
    }
    scan = {"totals": {"today_tokens": 1_204_800,
                       "today_by_model": usage, "week_by_model": usage},
            "projects": [], "bytes_read": 0}
    state = main.build_state(api_result=None, api_error=None,
                             scan_result=scan, scan_error=None)
    rows = state["cost"]["by_model"]
    assert sum(r["today_usd"] for r in rows) == pytest.approx(state["cost"]["today_usd"]), \
        "今日合計要等於各列今日金額相加"
    assert sum(r["week_usd"] for r in rows) == pytest.approx(state["cost"]["week_usd"]), \
        "本週合計要等於各列本週金額相加"
    assert state["cost"]["today_usd"] == pytest.approx(2.00), \
        "各列 1.00 + 1.00 = 2.00，不是原始值加總的 2.01"


# =====================================================================
# 2026-09-09 HANDOFF.md 的小尾巴（第四版交接的「剩下的小尾巴」1~3 條）。
# 契約：main._sync_history(cache_dir, projects_dir) -> List[str]
#   把 main.py 現有那段「帳本不存在就補建、更新 report.html、.broken 提醒」
#   的邏輯抽成這支函式，回傳這次要併進 state["errors"] 的訊息列表。
#   main() 呼叫它並把回傳值 extend 進 state["errors"]，行為不變（只是可測）。
# =====================================================================

def test_帳本損毀提醒只在剛壞掉那一輪出現(tmp_path):
    """HANDOFF 尾巴 1：.broken 備份檔一天不刪，提醒就不能跟著顯示一天，
    否則會變成永久噪音，蓋掉真正該被注意的新錯誤。
    """
    from collector import history
    cache = tmp_path / "cache"
    proj = tmp_path / "projects"
    cache.mkdir()
    history.record_day(cache, "2026-09-01", {}, project_tokens={})
    (cache / history.HISTORY_FILE).write_text("這不是合法的 JSON")

    first_errors = main._sync_history(cache, proj)
    assert any("損毀" in e for e in first_errors), "剛壞掉那一輪要提醒"

    second_errors = main._sync_history(cache, proj)
    assert not any("損毀" in e for e in second_errors), \
        "備份檔還在，但不是這一輪弄壞的，不該再提醒"


def test_帳本損毀當次要真的補建不是空等下一輪(tmp_path):
    """HANDOFF 尾巴 2：目前訊息說『已重新補建』，但實際上損毀當次報表是空的，
    要等下一輪才真的補建——訊息與事實不符。這裡要求損毀當次就補建完成。
    """
    from collector import history
    cache = tmp_path / "cache"
    proj = tmp_path / "projects"
    cache.mkdir()
    _write_row(proj / "-home-someone-Claude-demo" / "a.jsonl", 700)

    history.record_day(cache, "2026-09-01", {}, project_tokens={})
    (cache / history.HISTORY_FILE).write_text("壞掉的內容")

    main._sync_history(cache, proj)

    report = history.weekly_report(cache)
    assert report and sum(w["tokens"] for w in report) > 0, \
        "損毀當次就要重新補建完成，這一輪的報表不能是空的"


# --- HANDOFF 尾巴 3：報表可用性 -------------------------------------------

def test_報表的_token_數字要有千分位():
    """813384677 這種數字沒有千分位很難讀，畫面唯一的工作就是讓人看懂。"""
    from collector import history
    report = [{"week_start": "2026-09-01", "tokens": 813384677, "usd": 12.34,
              "by_model": {}, "days": ["2026-09-01"]}]
    html = history.render_html(report)
    assert "813,384,677" in html, "Token 數字要加千分位"


def test_報表的包含日期欄不逐日全列避免把_token_金額擠出畫面():
    """整週 7 天的日期全列出來會把 Token 與金額欄擠出畫面外要捲動，
    改成起訖日＋天數的精簡格式，但起訖日跟天數本身仍要看得到。
    """
    from collector import history
    days = [f"2026-09-{d:02d}" for d in range(1, 8)]  # 一整週 7 天
    report = [{"week_start": "2026-09-01", "tokens": 100, "usd": 1.0,
              "by_model": {}, "days": days}]
    html = history.render_html(report)
    assert "2026-09-01, 2026-09-02, 2026-09-03" not in html, \
        "七天的日期不該逐一列出撐開欄寬"
    assert "2026-09-01" in html and "2026-09-07" in html, \
        "起訖日仍要看得到"
    assert "7" in html, "至少要看得出這一週含幾天"
