"""來源 A：打 https://api.anthropic.com/api/oauth/usage 取得額度百分比。

規格見 SPEC.md §2.1 與 §4.1。
"""
import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path


def read_access_token(credentials_path=None):
    """從 ~/.claude/.credentials.json 讀出 accessToken。

    ⚠️ 只讀不寫。回傳值是機密，絕不可 log、print 或寫進任何檔案。
    讀不到時回傳 None（不要拋例外，呼叫端要能降級）。
    """
    if credentials_path is None:
        credentials_path = Path.home() / ".claude" / ".credentials.json"
    else:
        credentials_path = Path(credentials_path)

    try:
        with open(credentials_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("claudeAiOauth", {}).get("accessToken")
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def fetch(token, timeout=20):
    """呼叫 API，回傳解析後的 dict。失敗時拋出帶中文訊息的例外。"""
    if not token:
        raise ValueError("沒有 access token，無法呼叫 API")

    url = "https://api.anthropic.com/api/oauth/usage"
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
    }

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError("登入已過期，請在終端機執行一次 claude") from e
        raise RuntimeError(f"API 回傳錯誤 {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"無法連線到 API: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"API 回應非合法 JSON: {e}") from e


def _kind_to_label(kind: str) -> str:
    """將 API 的 kind 轉成中文標題。"""
    mapping = {
        "session": "本次 session",
        "weekly_all": "本週全部模型",
        "weekly_scoped": "本週特定模型",
    }
    return mapping.get(kind, kind)


def _to_taiwan_time(utc_str: str) -> str:
    """將 UTC ISO 字串轉成台灣時間（UTC+8）的 ISO 字串。"""
    dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tw_tz = timezone(timedelta(hours=8))
    dt_tw = dt.astimezone(tw_tz)
    return dt_tw.isoformat()


def _format_resets_in_text(resets_at_utc: str) -> str:
    """將重置時間轉成「X 小時 Y 分後」格式（台灣時間）。"""
    now_utc = datetime.now(timezone.utc)
    reset_dt = datetime.fromisoformat(resets_at_utc.replace("Z", "+00:00"))
    if reset_dt.tzinfo is None:
        reset_dt = reset_dt.replace(tzinfo=timezone.utc)

    diff = reset_dt - now_utc
    total_seconds = int(diff.total_seconds())

    if total_seconds <= 0:
        return "剛重置"

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60

    parts = []
    if hours > 0:
        parts.append(f"{hours} 小時")
    if minutes > 0 or hours == 0:
        parts.append(f"{minutes} 分")
    return " ".join(parts) + "後"  # 用空格分隔，否則會變成「2 小時49 分後」


def normalize_limits(api_result):
    """把 API 的 limits 陣列轉成 SPEC §4.1 的格式。

    每個元素：{label, percent, severity, resets_at, resets_in_text}
    - label：scope.model.display_name 優先，否則依 kind 對應中文，
      認不得的 kind 也要給非空標題（不可吞掉該條目）。
    - resets_at：換算成台灣時間（UTC+8）的 ISO 字串。
    """
    if not api_result or "limits" not in api_result:
        return []

    limits = api_result.get("limits", [])
    out = []

    for lim in limits:
        kind = lim.get("kind", "")
        group = lim.get("group", "")
        percent = lim.get("percent", 0)
        severity = lim.get("severity", "normal")
        resets_at = lim.get("resets_at", "")
        scope = lim.get("scope")
        is_active = lim.get("is_active", False)

        # 決定 label：優先用 scope.model.display_name
        label = None
        if scope and isinstance(scope, dict):
            model = scope.get("model")
            if model and isinstance(model, dict):
                display_name = model.get("display_name")
                if display_name:
                    label = display_name

        if not label:
            label = _kind_to_label(kind)

        # 轉換時間
        resets_at_tw = _to_taiwan_time(resets_at) if resets_at else ""
        resets_in_text = _format_resets_in_text(resets_at) if resets_at else ""

        out.append({
            "label": label,
            "percent": percent,
            "severity": severity,
            "resets_at": resets_at_tw,
            "resets_in_text": resets_in_text,
        })

    return out