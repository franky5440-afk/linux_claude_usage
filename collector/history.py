"""歷史週報的日結帳本。

背景見 HANDOFF.md A 節：增量掃描手上永遠只有今天，要看歷史必須先留下來。
解法是搭跨日快取作廢的便車——丟掉今日累計之前先寫進這本帳本，一天一筆。

帳本檔：cache_dir/history.json，格式：
  {"schema_version": 1, "days": {date_str: {"by_model": {...}, "projects": {...}}}}

規則：
- record_day 是覆蓋不是累加（collector 每 60 秒跑一次，同一天會記很多次）。
- 週報按台灣時間的週一分週。
- 金額一律走 pricing.estimate_cost，不另寫算法。
- 資料沒有就是沒有，不拿別的數字頂替。
"""
import html
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from collector import transcript_scan

TW = timezone(timedelta(hours=8))

HISTORY_FILE = "history.json"
BROKEN_FILE = "history.json.broken"
SCHEMA_VERSION = 1

_USAGE_KEYS = ("input_tokens", "output_tokens",
               "cache_creation_input_tokens", "cache_read_input_tokens")


def _history_path(cache_dir) -> Path:
    return Path(cache_dir) / HISTORY_FILE


def _clean_usage(usage: Dict[str, Any]) -> Dict[str, int]:
    """只保留四個分項計數，其餘欄位丟掉，避免帳本混入雜物。"""
    cleaned = {}
    for k in _USAGE_KEYS:
        try:
            cleaned[k] = int((usage or {}).get(k, 0))
        except (TypeError, ValueError):
            cleaned[k] = 0
    return cleaned


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, path)
    except OSError:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def record_day(cache_dir, date_str: str, by_model: Dict[str, Dict[str, int]],
               project_tokens=None) -> None:
    """寫入某一天的日結，同一天重複寫入直接覆蓋。

    by_model：{model: usage_dict}，usage_dict 含四個分項計數。
    project_tokens：{專案名: token 數}，沒有就傳空 dict，不拿別的數字頂替。
    """
    path = _history_path(cache_dir)
    store = load(cache_dir)
    days = store["days"]

    cleaned_models = {}
    for model, usage in (by_model or {}).items():
        cleaned_models[model] = _clean_usage(usage)

    cleaned_projects = {}
    for name, tokens in (project_tokens or {}).items():
        try:
            cleaned_projects[name] = int(tokens)
        except (TypeError, ValueError):
            raise ValueError(f"專案 {name} 的 token 數不是整數，拒絕寫入")

    days[date_str] = {"by_model": cleaned_models, "projects": cleaned_projects}
    _atomic_write(path, {"schema_version": SCHEMA_VERSION, "days": days})


def _empty_store() -> Dict[str, Any]:
    """回傳空帳本。資料沒有就是沒有，不編造數字。"""
    return {"schema_version": SCHEMA_VERSION, "days": {}}


def _mark_broken(path: Path) -> None:
    """帳本損毀時改名備份，讓 main.py 能發現並寫進 state 的錯誤訊息。"""
    broken = path.with_name(BROKEN_FILE)
    try:
        if broken.exists():
            broken.unlink()
        os.replace(path, broken)
    except OSError as e:
        raise ValueError(f"歷史帳本損毀且無法備份: {e}")


def load(cache_dir) -> Dict[str, Any]:
    """讀出整本帳本。檔案不存在就回空帳本。

    檔案內容損毀時，先改名為 history.json.broken 備份再回空帳本，
    上層靠該備份檔得知曾經壞過（見 main.py）。備份本身失敗才拋錯。
    """
    path = _history_path(cache_dir)
    if not path.exists():
        return _empty_store()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, ValueError):
        _mark_broken(path)
        return _empty_store()
    except OSError:
        return _empty_store()
    days = data.get("days") if isinstance(data, dict) else None
    if not isinstance(days, dict):
        _mark_broken(path)
        return _empty_store()
    return {"schema_version": SCHEMA_VERSION, "days": days}


def _week_start(date_str: str) -> str:
    """回傳該日期所在週的週一。週界定義只有 transcript_scan._monday_of 一套。"""
    return transcript_scan._monday_of(date.fromisoformat(date_str)).isoformat()


def weekly_report(cache_dir) -> List[Dict[str, Any]]:
    """把日結按週一分週合併，每週一筆，依 week_start 由舊到新排序。

    每筆：{"week_start": str, "tokens": int, "usd": float,
           "by_model": {...}, "days": [...]}
    金額走 pricing.estimate_cost，與今日成本同一套算法。
    """
    from collector import pricing

    days = load(cache_dir)["days"]
    weeks: Dict[str, Dict[str, Any]] = {}
    for date_str in sorted(days):
        try:
            start = _week_start(date_str)
        except ValueError:
            continue
        entry = days[date_str] or {}
        by_model = entry.get("by_model") or {}
        week = weeks.setdefault(start, {"by_model": {}, "days": []})
        week["days"].append(date_str)
        for model, usage in by_model.items():
            cleaned = _clean_usage(usage)
            bucket = week["by_model"].setdefault(
                model, {k: 0 for k in _USAGE_KEYS})
            for k in _USAGE_KEYS:
                bucket[k] += cleaned[k]

    table = pricing.load_table()
    report = []
    for start in sorted(weeks):
        merged = weeks[start]["by_model"]
        tokens = sum(sum(u.values()) for u in merged.values())
        usd, _ = pricing.estimate_cost(merged, table)
        report.append({
            "week_start": start,
            "tokens": tokens,
            "usd": usd,
            "by_model": merged,
            "days": weeks[start]["days"],
        })
    return report


def rebuild(cache_dir, projects_dir) -> Dict[str, int]:
    """一次性全量掃描，把過去每一天補進帳本（只跑一次，不違反 SPEC §3）。

    回傳 {date_str: tokens} 表示這次補了哪些天。時間戳無效的行直接跳過，
    不拿別的日期頂替。
    """
    projects_dir = Path(projects_dir)
    per_day_by_model: Dict[str, Dict[str, Dict[str, int]]] = {}
    per_day_projects: Dict[str, Dict[str, int]] = {}

    if not projects_dir.exists():
        return {}

    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        if not proj_dir.name.startswith("-home-"):
            continue
        project_name = transcript_scan._decode_project_name(proj_dir.name)
        for jsonl_file in proj_dir.rglob("*.jsonl"):
            try:
                with open(jsonl_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line in lines:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message", {})
                model = msg.get("model", "unknown")
                usage = msg.get("usage", {})
                ts = obj.get("timestamp")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    day_str = dt.astimezone(TW).date().isoformat()
                except (ValueError, AttributeError):
                    continue
                counts = _clean_usage(usage)
                day_models = per_day_by_model.setdefault(day_str, {})
                bucket = day_models.setdefault(
                    model, {k: 0 for k in _USAGE_KEYS})
                for k in _USAGE_KEYS:
                    bucket[k] += counts[k]
                tokens = sum(counts.values())
                day_projects = per_day_projects.setdefault(day_str, {})
                day_projects[project_name] = day_projects.get(project_name, 0) + tokens

    result = {}
    for day_str in sorted(per_day_by_model):
        record_day(cache_dir, day_str,
                   per_day_by_model[day_str],
                   project_tokens=per_day_projects.get(day_str, {}))
        result[day_str] = sum(sum(u.values())
                              for u in per_day_by_model[day_str].values())
    return result


def render_html(report: List[Dict[str, Any]]) -> str:
    """把週報畫成自足的 HTML（內嵌樣式，不外連，離線也能看）。"""
    rows = []
    for week in report:
        week_start = html.escape(str(week.get("week_start", "")))
        days = week.get("days", []) or []
        if not days:
            day_text = week_start
        elif len(days) == 1:
            day_text = html.escape(str(days[0]))
        else:
            ordered = sorted(str(d) for d in days)
            day_text = (f"{html.escape(ordered[0])} ~ "
                        f"{html.escape(ordered[-1])}（{len(ordered)} 天）")
        tokens = week.get("tokens", 0)
        usd = week.get("usd", 0.0)
        tokens_text = f"{int(tokens):,}"
        try:
            usd_text = f"${float(usd):.2f}"
        except (TypeError, ValueError):
            usd_text = "—"
        rows.append(
            f"<tr><td>{week_start}</td><td>{day_text}</td>"
            f"<td>{tokens_text}</td><td>{usd_text}</td></tr>")
    body = "\n".join(rows) if rows else (
        '<tr><td colspan="4">尚無歷史資料</td></tr>')
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-Hant">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        "<title>每週用量報表</title>\n"
        "<style>\n"
        "body{font-family:sans-serif;margin:2em;color:#222;background:#fff}\n"
        "table{border-collapse:collapse;width:100%}\n"
        "th,td{border:1px solid #999;padding:0.4em 0.6em;text-align:left}\n"
        "th{background:#eee}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        "<h1>每週用量報表</h1>\n"
        "<p>金額為參考估算值。</p>\n"
        "<table>\n"
        "<tr><th>週一</th><th>包含日期</th><th>Token</th><th>金額</th></tr>\n"
        f"{body}\n"
        "</table>\n"
        "</body>\n"
        "</html>\n"
    )
