"""組裝 state.json 並寫到 ~/.cache/claude-usage-widget/state.json（權限 0600）。"""
import json
import os
import time
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# 允許直接執行 python collector/main.py
sys.path.insert(0, str(Path(__file__).parent.parent))

from collector import usage_api, transcript_scan, pricing, history, session_context

STATE_DIR = Path.home() / ".cache" / "claude-usage-widget"
STATE_FILE = STATE_DIR / "state.json"

TW = timezone(timedelta(hours=8))


def _now_iso() -> str:
    """取得當下台灣時間的 ISO 字串。"""
    return datetime.now(TW).isoformat()


def _default_state() -> Dict[str, Any]:
    """回傳預設的空 state 結構。"""
    return {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "ok": False,
        "errors": [],
        "limits": [],
        "cost": {
            "today_usd": 0.0,
            "week_usd": 0.0,
            "pricing_version": "unknown",
            "by_model": [],
        },
        "projects": [],
        "sessions": [],
        "totals": {
            "today_tokens": 0,
            "today_by_model": {},
        },
    }


def build_state(api_result: Optional[Dict[str, Any]], api_error: Optional[str],
                scan_result: Optional[Dict[str, Any]], scan_error: Optional[str],
                sessions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """產生符合 SPEC §4.1 的 state dict。

    ⚠️ 任何一邊失敗，其餘欄位仍須存在（可為空陣列 / null），
    且必須可以被 json.dumps 序列化。
    """
    state = _default_state()

    errors: List[str] = []

    # 處理 API 結果
    if api_error:
        errors.append(api_error)
    elif api_result:
        limits = usage_api.normalize_limits(api_result)
        state["limits"] = limits

    # 處理掃描結果
    if scan_error:
        errors.append(scan_error)
    elif scan_result:
        totals = scan_result.get("totals", {})
        projects = scan_result.get("projects", [])

        state["totals"]["today_tokens"] = totals.get("today_tokens", 0)
        state["totals"]["today_by_model"] = totals.get("today_by_model", {})

        state["projects"] = projects

        # 計算成本（分項與總額來自同一套算法，見 SPEC §6.1）
        today_usage = totals.get("today_by_model", {})
        table = pricing.load_table()
        today_per, cost_errors = pricing.estimate_cost_by_model(today_usage, table)
        errors.extend(cost_errors)

        state["cost"]["pricing_version"] = table.get("version", "unknown")

        # 本週成本：最近 7 天（含今天）的用量換算，價格表與今日成本共用。
        # scan() 保證回傳 week_by_model；缺欄位不粉飾成假數字，
        # 降級為 None 並留一條看得懂的訊息（SPEC §4.2）。
        try:
            week_usage = totals["week_by_model"]
        except KeyError:
            errors.append("掃描結果缺少本週用量，本週成本無法計算")
            state["cost"]["week_usd"] = None
            week_per: Dict[str, float] = {}
        else:
            week_per, week_errors = pricing.estimate_cost_by_model(week_usage, table)
            for e in week_errors:
                if e not in errors:
                    errors.append(e)

        # 分模型明細：依本週金額由大到小。只列有用量且有價格的模型
        # （沒用量／沒價格的進不了分項 dict，錯誤訊息在上面照舊處理）。
        # 合計＝各列四捨五入後相加（Frank 2026-09-09 拍板），讓畫面自己加得起來；
        # 列與合計來自同一組分項，沒有金額被弄丟，只是換了取整順序。
        price_models = table.get("models", {})
        rows = []
        for model_name in set(today_per) | set(week_per):
            rows.append({
                "model": model_name,
                "label": price_models.get(model_name, {}).get("display", model_name),
                "today_usd": round(today_per.get(model_name, 0.0), 2),
                "week_usd": round(week_per.get(model_name, 0.0), 2),
            })
        # 依本週金額由大到小；平手時用 model 名當第二鍵，
        # 否則 set 迭代順序不穩定，畫面上的列會無故換位置。
        rows.sort(key=lambda r: (-r["week_usd"], r["model"]))
        state["cost"]["by_model"] = rows

        state["cost"]["today_usd"] = round(sum(r["today_usd"] for r in rows), 2)
        if state["cost"]["week_usd"] is not None:
            state["cost"]["week_usd"] = round(sum(r["week_usd"] for r in rows), 2)

    # 如果兩邊都成功，ok = True
    if not api_error and not scan_error:
        state["ok"] = True

    # D 區塊：collector 算好就好，desklet 只負責畫；缺資料就給空陣列
    state["sessions"] = list(sessions) if sessions is not None else []

    state["errors"] = errors
    state["generated_at"] = _now_iso()

    return state


def write_state(state: Dict[str, Any]) -> None:
    """將 state 寫入 state.json，權限設為 0600。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_FILE.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, STATE_FILE)
    except OSError:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


# API 節流：SPEC §7 規定額度 API 每 5 分鐘才打一次。
# desklet 的更新間隔可以短到 10 秒，若每次都打會被回 429 Too Many Requests
# （2026-09-08 實際踩過）。所以把上次結果快取下來，時間未到就直接沿用。
API_MIN_INTERVAL_SECONDS = 300
# 被回 429 之後拉長到這個間隔才再試，避免持續撞牆
API_BACKOFF_SECONDS = 900


def _api_cache_path() -> Path:
    return STATE_DIR / "api_cache.json"


def _load_api_cache():
    """讀 API 快取。回傳 (資料, 上次抓取時間戳, 上次是否被限流)。"""
    try:
        with open(_api_cache_path()) as fh:
            c = json.load(fh)
        return c.get("result"), float(c.get("fetched_at", 0)), bool(c.get("rate_limited"))
    except Exception:
        return None, 0.0, False


def _save_api_cache(result, rate_limited: bool) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _api_cache_path()
        with open(path, "w") as fh:
            json.dump({"result": result, "fetched_at": time.time(),
                       "rate_limited": rate_limited}, fh, ensure_ascii=False)
        os.chmod(path, 0o600)
    except Exception:
        pass  # 快取寫不進去不該讓整支程式失敗


def fetch_usage_throttled():
    """取得額度資料，遵守 §7 的呼叫頻率。回傳 (結果, 錯誤訊息)。"""
    cached, fetched_at, was_limited = _load_api_cache()
    age = time.time() - fetched_at
    min_interval = API_BACKOFF_SECONDS if was_limited else API_MIN_INTERVAL_SECONDS

    if cached is not None and age < min_interval:
        return cached, None  # 還在節流窗口內，沿用上次結果

    token = usage_api.read_access_token()
    if not token:
        return cached, "找不到憑證檔或 accessToken"

    try:
        result = usage_api.fetch(token)
        _save_api_cache(result, rate_limited=False)
        return result, None
    except Exception as e:
        msg = str(e)
        limited = "429" in msg
        if cached is not None:
            _save_api_cache(cached, rate_limited=limited)
            note = "額度 API 被限流，顯示的是上次取得的資料" if limited \
                else "額度 API 呼叫失敗，顯示的是上次取得的資料"
            return cached, f"{note}（{msg}）"
        _save_api_cache(None, rate_limited=limited)
        return None, msg


def _sync_history(cache_dir: Path, projects_dir: Path) -> List[str]:
    """更新歷史帳本與 report.html，回傳這輪要併入 state["errors"] 的訊息。"""
    errors: List[str] = []
    cache_path = Path(cache_dir)
    broken_path = cache_path / history.BROKEN_FILE
    broken_before = broken_path.exists()
    try:
        if not (cache_path / history.HISTORY_FILE).exists():
            history.rebuild(cache_dir=cache_dir, projects_dir=projects_dir)
        report = history.weekly_report(cache_dir)
        cache_path.mkdir(parents=True, exist_ok=True)
        with open(cache_path / "report.html", "w", encoding="utf-8") as f:
            f.write(history.render_html(report))
    except Exception as e:
        errors.append(f"歷史週報產生失敗: {e}")
    if not broken_before and broken_path.exists():
        try:
            history.rebuild(cache_dir=cache_dir, projects_dir=projects_dir)
            report = history.weekly_report(cache_dir)
            cache_path.mkdir(parents=True, exist_ok=True)
            with open(cache_path / "report.html", "w", encoding="utf-8") as f:
                f.write(history.render_html(report))
        except Exception as e:
            errors.append(f"歷史週報產生失敗: {e}")
        errors.append(
            "歷史帳本檔案損毀，已另存為 history.json.broken 並重新補建")
    return errors


def main() -> int:
    """主程式入口。"""
    api_result = None
    api_error = None
    scan_result = None
    scan_error = None

    # 取得額度資料（有節流，見 fetch_usage_throttled）
    api_result, api_error = fetch_usage_throttled()

    # 掃描逐字稿
    cache_dir = STATE_DIR
    projects_dir = Path.home() / ".claude" / "projects"
    try:
        scan_result = transcript_scan.scan(cache_dir=cache_dir, projects_dir=projects_dir)
    except Exception as e:
        scan_error = f"逐字稿掃描失敗: {e}"

    # D 區塊：單一 session 的 context 佔用（SPEC §10）。附加產物，
    # 失敗只記進 errors，不讓整支掛掉（SPEC §4.2）。
    sessions: List[Dict[str, Any]] = []
    session_error: Optional[str] = None
    try:
        sessions = session_context.active_sessions(projects_dir)
    except Exception as e:
        session_error = f"活動 session 掃描失敗：{e}"

    # 組裝 state
    state = build_state(api_result, api_error, scan_result, scan_error,
                        sessions=sessions)
    if session_error is not None:
        state["errors"].append(session_error)

    # 歷史週報：帳本不存在就先全量補建一次，之後每次更新報表。
    # 週報是附加產物，失敗只記進 errors，不影響 state 主體與結束碼。
    state["errors"].extend(_sync_history(cache_dir, projects_dir))
    write_state(state)

    return 0


if __name__ == "__main__":
    exit(main())