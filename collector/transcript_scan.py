"""來源 B：增量掃描 ~/.claude/projects/**/*.jsonl 統計 token。

規格見 SPEC.md §2.2、§3（效能硬需求：禁止全量掃描）。
"""
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Tuple


TW = timezone(timedelta(hours=8))

CACHE_FILE = "file_offsets.json"
TOTALS_CACHE = "totals_cache.json"


def _load_cache(cache_dir: Path) -> Dict[str, Any]:
    """載入快取檔。"""
    cache_path = cache_dir / CACHE_FILE
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"files": {}, "totals": {}}


def _save_cache(cache_dir: Path, cache_data: Dict[str, Any]) -> None:
    """儲存快取檔（原子寫入）。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / CACHE_FILE
    tmp_path = cache_path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False)
        os.replace(tmp_path, cache_path)
    except OSError:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _load_totals_cache(cache_dir: Path) -> Dict[str, Any]:
    """載入累計值快取。"""
    cache_path = cache_dir / TOTALS_CACHE
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_totals_cache(cache_dir: Path, totals: Dict[str, Any]) -> None:
    """儲存累計值快取。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / TOTALS_CACHE
    tmp_path = cache_path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(totals, f, ensure_ascii=False)
        os.replace(tmp_path, cache_path)
    except OSError:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _decode_project_name(dir_name: str) -> str:
    """將目錄名反解成專案名。
    支援三種格式：
      -home-<user>                          -> "家目錄"
      -home-<user>-Claude-<專案>            -> <專案> (專案名可能含 -)
      -home-<user>-<其他前綴>-<專案>        -> <專案> (取最後一段)
    """
    if not dir_name.startswith("-home-"):
        return dir_name
    
    # 去掉 -home-<user>- 前綴 (分割成 4 部分：['', 'home', '<user>', '<rest>'])
    parts = dir_name.split("-", 3)
    if len(parts) < 4 or not parts[3]:
        # 沒有 rest 部分，代表是 -home-<user> 這種家目錄本身
        return "家目錄"
    
    rest = parts[3]  # 去掉 -home-<user>- 後的剩餘部分
    
    # 如果是 -home-<user>-Claude-<專案> 格式
    if rest.startswith("Claude-"):
        return rest[len("Claude-"):]  # 保留完整專案名（可能含 -）
    
    # 其他格式：取最後一段作為專案名
    return rest.split("-")[-1]


def _parse_jsonl_line(line: str) -> Tuple[bool, Dict[str, Any]]:
    """解析單行 jsonl，回傳 (是否為 assistant, 解析後的資料)。"""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return False, {}

    if obj.get("type") != "assistant":
        return False, {}

    msg = obj.get("message", {})
    model = msg.get("model", "unknown")
    usage = msg.get("usage", {})
    timestamp = obj.get("timestamp")

    return True, {
        "model": model,
        "usage": usage,
        "timestamp": timestamp,
    }


def _is_today(timestamp_str: str) -> bool:
    """判斷 timestamp（UTC）是否為今天（台灣時間）。"""
    try:
        dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_tw = dt.astimezone(TW)
        today_tw = datetime.now(TW).date()
        return dt_tw.date() == today_tw
    except (ValueError, AttributeError):
        return False


def _today_str() -> str:
    """回傳今天日期（台灣時間）的 YYYY-MM-DD 字串。"""
    return datetime.now(TW).date().isoformat()


def _monday_of(day):
    """該日期所在週的週一。週界定義只有這一套，history 共用。"""
    return day - timedelta(days=day.weekday())


def _is_in_week(timestamp_str: str) -> bool:
    """判斷 timestamp（UTC）是否落在「本週一到今天」（台灣時間切日）。"""
    try:
        dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        day_tw = dt.astimezone(TW).date()
        today_tw = datetime.now(TW).date()
        if day_tw > today_tw:
            return False
        return _monday_of(day_tw) == _monday_of(today_tw)
    except (ValueError, AttributeError):
        return False


def _process_file(filepath: Path, last_offset: int) -> Tuple[int, List[Dict[str, Any]]]:
    """處理單一檔案，從 last_offset 開始讀取，回傳 (新 offset, 記錄列表)。"""
    records = []
    new_offset = last_offset

    try:
        with open(filepath, "rb") as f:
            f.seek(last_offset)
            while True:
                line_bytes = f.readline()
                if not line_bytes:
                    break
                new_offset = f.tell()
                try:
                    line = line_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                is_assistant, data = _parse_jsonl_line(line.strip())
                if is_assistant:
                    records.append(data)
    except OSError:
        pass

    return new_offset, records


def scan(cache_dir, projects_dir):
    """增量掃描，回傳：

    {
      "bytes_read": int,          # 這一輪實際讀了幾個位元組（沒變動就是 0）
      "totals": {"today_tokens": int, "today_by_model": {model: usage_dict},
                 "week_by_model": {model: usage_dict}},
      "projects": [{"name": str, "tokens": int, "percent": float}],
    }

    快取（每個檔的 size/mtime/offset 與累計值）寫在 cache_dir 底下。
    累計值只在同一天（台灣時間）有效，跨日一律作廢重算。
    """

    def _empty_usage() -> Dict[str, int]:
        # 單一模型的四個分項計數器
        return {"input_tokens": 0, "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0}

    def _merge_usage(bucket: Dict[str, Dict[str, int]], model: str,
                     counts: Dict[str, int]) -> None:
        # 將 counts 累加進 bucket[model]
        if model not in bucket:
            bucket[model] = _empty_usage()
        for k, v in counts.items():
            bucket[model][k] = bucket[model].get(k, 0) + v

    cache_dir = Path(cache_dir)
    projects_dir = Path(projects_dir)

    # 載入快取
    cache = _load_cache(cache_dir)
    file_cache = cache.get("files", {})
    totals_cache = _load_totals_cache(cache_dir)

    # P0：今日累計只在同一天有效。快取若缺 scan_date 或日期不是今天，
    # 代表隔夜（或舊版快取），今日累計與 offset 一併作廢，從頭重算。
    today_str = _today_str()
    if totals_cache.get("scan_date") != today_str:
        # 跨日作廢前先把舊的今日累計歸檔到日結帳本（HANDOFF.md A 節的關鍵接縫）。
        # 沒有日期或沒有用量就沒有東西可歸檔，該空就空，不編造。
        stale_date = totals_cache.get("scan_date")
        stale_by_model = totals_cache.get("today_by_model") or {}
        if stale_date and stale_by_model:
            from collector import history
            history.record_day(
                cache_dir, stale_date, stale_by_model,
                project_tokens=totals_cache.get("project_tokens") or {})
        file_cache = {}

    today_by_model: Dict[str, Dict[str, int]] = {}
    week_by_model: Dict[str, Dict[str, int]] = {}
    project_tokens: Dict[str, int] = {}
    total_bytes_read = 0

    if not projects_dir.exists():
        # 專案目錄不存在，回傳空結果但保留快取
        return {
            "bytes_read": 0,
            "totals": {
                "today_tokens": 0,
                "today_by_model": {},
                "week_by_model": {},
            },
            "projects": [],
        }

    # 掃描所有專案目錄（以 -home- 開頭的目錄）
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        # 掃描所有 -home-<user> 開頭的目錄，不限制特定使用者或路徑格式
        if not proj_dir.name.startswith("-home-"):
            continue

        project_name = _decode_project_name(proj_dir.name)
        project_total = 0

        # 使用 rglob 遞迴掃描所有子目錄下的 .jsonl 檔案
        for jsonl_file in proj_dir.rglob("*.jsonl"):
            try:
                stat = jsonl_file.stat()
            except OSError:
                continue

            file_key = str(jsonl_file)
            cached = file_cache.get(file_key, {})
            last_size = cached.get("size", 0)
            last_mtime = cached.get("mtime", 0)
            last_offset = cached.get("offset", 0)

            current_size = stat.st_size
            current_mtime = int(stat.st_mtime)

            # 檢查檔案是否有變動
            if current_size == last_size and current_mtime == last_mtime:
                # 檔案沒變，直接用快取的累計值
                for model, usage in cached.get("today_by_model", {}).items():
                    _merge_usage(today_by_model, model, usage)
                for model, usage in cached.get("week_by_model", {}).items():
                    _merge_usage(week_by_model, model, usage)
                cached_project_tokens = cached.get("project_tokens", 0)
                project_total += cached_project_tokens
                project_tokens[project_name] = project_tokens.get(project_name, 0) + cached_project_tokens
                continue

            # 檔案有變動
            if current_size < last_size:
                # 檔案變小了（可能被截斷），從頭讀
                last_offset = 0
                # 截斷時不保留舊快取資料
                cached_today_by_model = {}
                cached_week_by_model = {}
                cached_project_tokens = 0
            else:
                # 檔案變大，保留舊快取資料
                cached_today_by_model = cached.get("today_by_model", {})
                cached_week_by_model = cached.get("week_by_model", {})
                cached_project_tokens = cached.get("project_tokens", 0)
                # 將舊快取資料加入全域累計
                for model, usage in cached_today_by_model.items():
                    _merge_usage(today_by_model, model, usage)
                for model, usage in cached_week_by_model.items():
                    _merge_usage(week_by_model, model, usage)
                project_total += cached_project_tokens
                # project_tokens 稍後由 file_project_tokens 累加，這裡不重複加

            new_offset, records = _process_file(jsonl_file, last_offset)
            bytes_read = new_offset - last_offset
            total_bytes_read += bytes_read

            # 更新檔案快取：從舊快取開始累加
            file_today_by_model: Dict[str, Dict[str, int]] = {}
            for model, usage in cached_today_by_model.items():
                file_today_by_model[model] = dict(usage)
            file_week_by_model: Dict[str, Dict[str, int]] = {}
            for model, usage in cached_week_by_model.items():
                file_week_by_model[model] = dict(usage)
            file_project_tokens = cached_project_tokens

            for rec in records:
                model = rec["model"]
                usage = rec["usage"]

                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                cache_creation = usage.get("cache_creation_input_tokens", 0)
                cache_read = usage.get("cache_read_input_tokens", 0)
                counts = {"input_tokens": input_tokens,
                          "output_tokens": output_tokens,
                          "cache_creation_input_tokens": cache_creation,
                          "cache_read_input_tokens": cache_read}

                if _is_in_week(rec["timestamp"]):
                    _merge_usage(week_by_model, model, counts)
                    _merge_usage(file_week_by_model, model, counts)

                if not _is_today(rec["timestamp"]):
                    continue

                _merge_usage(today_by_model, model, counts)
                _merge_usage(file_today_by_model, model, counts)

                tokens = input_tokens + output_tokens + cache_creation + cache_read
                file_project_tokens += tokens
                project_total += tokens

            # 更新快取
            file_cache[file_key] = {
                "size": current_size,
                "mtime": current_mtime,
                "offset": new_offset,
                "today_by_model": file_today_by_model,
                "week_by_model": file_week_by_model,
                "project_tokens": file_project_tokens,
            }

            project_tokens[project_name] = project_tokens.get(project_name, 0) + file_project_tokens

    # 計算總 token
    today_tokens = 0
    for model_usage in today_by_model.values():
        today_tokens += sum(model_usage.values())

    # 計算專案佔比（過濾掉 tokens 為 0 的專案）
    projects_list = []
    for name, tokens in sorted(project_tokens.items(), key=lambda x: -x[1]):
        if tokens <= 0:
            continue
        percent = (tokens / today_tokens * 100) if today_tokens > 0 else 0.0
        projects_list.append({
            "name": name,
            "tokens": tokens,
            "percent": round(percent, 1),
        })

    # 只取 Top 5
    projects_list = projects_list[:5]

    # 儲存快取
    cache["files"] = file_cache
    cache["scan_date"] = today_str
    _save_cache(cache_dir, cache)

    totals_data = {
        "scan_date": today_str,
        "today_by_model": today_by_model,
        "week_by_model": week_by_model,
        "project_tokens": project_tokens,
    }
    _save_totals_cache(cache_dir, totals_data)

    return {
        "bytes_read": total_bytes_read,
        "totals": {
            "today_tokens": today_tokens,
            "today_by_model": today_by_model,
            "week_by_model": week_by_model,
        },
        "projects": projects_list,
    }