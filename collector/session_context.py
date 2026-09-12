"""單一 session 的 context 佔用（SPEC §10／§11）。

讀 ~/.claude/projects/**/*.jsonl，但不走增量掃描那條路：
這裡要的是「最後一則的當下值」，不是全部歷史的累加。

契約以 tests/test_session_context.py 為準，函式簽章不得更動。
"""
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from collector import transcript_scan

# 沒有 [1m] 標記時的預設 context window
DEFAULT_CONTEXT_WINDOW = 200_000
LONG_CONTEXT_WINDOW = 1_000_000

# 台灣時間（SPEC §4.1：時間欄位進 state 之前就換算完）
TW = timezone(timedelta(hours=8))

# 效能硬約束（SPEC §10.3）：只讀檔頭與檔尾各這麼多，不要整檔讀進記憶體
HEAD_BYTES = 256 * 1024
TAIL_BYTES = 256 * 1024


def context_tokens(usage: Dict[str, Any]) -> int:
    """算出這一輪送進模型的 context 長度。

    ＝ input_tokens + cache_read_input_tokens + cache_creation_input_tokens
    output_tokens 不算（它要到下一輪才進 context）。
    """
    return ((usage.get("input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0))


def _read_relevant_lines(path: Path) -> List[str]:
    """只讀檔頭與檔尾各數百 KB，回傳解開的文字行（照檔內順序）。

    中間沒讀到的部分直接跳過；頭尾交界處切半的行會丟掉，不拿半行去解析。
    """
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if size <= HEAD_BYTES + TAIL_BYTES:
            # 小檔：一次讀完（本來就不大，不算全量掃描）
            text = f.read()
        else:
            head = f.read(HEAD_BYTES)
            # 頭部多補到行尾，避免最後一行是半行
            head += f.readline(65536)
            f.seek(size - TAIL_BYTES)
            tail = f.read()
            # 尾部丟掉開頭的半行
            nl = tail.find(b"\n")
            if nl != -1:
                tail = tail[nl + 1:]
            text = head + tail
    return text.decode("utf-8", errors="replace").splitlines()


def _scan_session_file(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """掃一個逐字稿檔，回傳（最後一則主線 usage，不存在則為 None；最後一筆 modelId）。

    modelId 以最後出現的那一筆為準（session 中途可能 /model 換過）。
    查不到 modelId 就回 None，不准拿別的數字頂替（SPEC §10.1）。
    """
    last_usage: Optional[Dict[str, Any]] = None
    model_id: Optional[str] = None
    for line in _read_relevant_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        record_type = obj.get("type")
        if record_type == "assistant":
            # 子代理（isSidechain）的用量是它自己的 context，不混進主線
            if obj.get("isSidechain"):
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            last_usage = usage
        elif record_type == "attachment":
            attachment = obj.get("attachment")
            if not isinstance(attachment, dict) or attachment.get("type") != "model":
                continue
            identity = attachment.get("identity")
            if not isinstance(identity, dict):
                continue
            mid = identity.get("modelId")
            if isinstance(mid, str) and mid:
                model_id = mid
    return last_usage, model_id


def active_sessions(projects_dir: Path, window_minutes: int = 5,
                    limit: int = 3) -> List[Dict[str, Any]]:
    """回傳窗口內還在活動的 session，依最近活動由新到舊，最多 limit 條。

    每一條的欄位（不得增減鍵名）：
        project          str            專案短名（反解自目錄名）
        tokens           int            當下 context 佔用
        context_window   int | None     分母；查不到 modelId 時為 None
        percent          float | None   佔比，四捨五入到小數一位；分母 None 時為 None
        model            str | None     modelId 原字串
        last_active_at   str            台灣時間 ISO 字串
    """
    projects_dir = Path(projects_dir)
    if not projects_dir.exists():
        return []

    # 先用 os.stat 的 mtime 篩掉窗口外的檔：篩掉的檔連 open 都不准開（SPEC §3）
    now = time.time()
    window_seconds = window_minutes * 60
    candidates: List[Tuple[float, Path, str]] = []
    try:
        project_dirs = list(projects_dir.iterdir())
    except OSError:
        return []
    for proj_dir in project_dirs:
        if not proj_dir.is_dir():
            continue
        try:
            session_files = list(proj_dir.rglob("*.jsonl"))
        except OSError:
            continue
        for session_file in session_files:
            try:
                mtime = os.stat(session_file).st_mtime
            except OSError:
                continue
            if now - mtime > window_seconds:
                continue
            candidates.append((mtime, session_file, proj_dir.name))

    # 依最近活動由新到舊
    candidates.sort(key=lambda item: item[0], reverse=True)

    rows: List[Dict[str, Any]] = []
    for mtime, session_file, dir_name in candidates:
        try:
            last_usage, model_id = _scan_session_file(session_file)
        except OSError:
            # 單一檔案讀不到就跳過，不讓整批掛掉
            continue
        if last_usage is None:
            # 還沒對話的 session 沒有數字可顯示，不列入
            continue
        tokens = context_tokens(last_usage)
        if model_id is None:
            context_window = None
            percent = None
        elif "[1m]" in model_id:
            context_window = LONG_CONTEXT_WINDOW
            percent = round(tokens / context_window * 100, 1)
        else:
            context_window = DEFAULT_CONTEXT_WINDOW
            percent = round(tokens / context_window * 100, 1)
        rows.append({
            # 專案名與 C 區塊（專案排行）共用同一套反解，畫面上不得出現兩種名字
            "project": transcript_scan._decode_project_name(dir_name),
            "tokens": tokens,
            "context_window": context_window,
            "percent": percent,
            "model": model_id,
            "last_active_at": datetime.fromtimestamp(mtime, tz=TW).isoformat(),
        })
        if len(rows) >= limit:
            break
    return rows
