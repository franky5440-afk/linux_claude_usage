"""介面契約測試 — 單一 session 的 context 佔用（SPEC §11）。

⚠️ builder 不得修改本檔的斷言。測試錯了請回報，不要自己改綠。
每個測試上方的註解說明「為什麼」這條契約存在。
"""
import builtins
import io
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collector import main, session_context, transcript_scan  # noqa: E402


# --- 造假逐字稿的工具 -----------------------------------------------------

def _assistant(usage, sidechain=False, ts="2026-09-13T01:00:00.000Z"):
    return {"type": "assistant", "isSidechain": sidechain, "timestamp": ts,
            "message": {"model": "claude-opus-5", "usage": usage}}


def _model_attachment(model_id):
    return {"type": "attachment", "isSidechain": False,
            "attachment": {"type": "model",
                           "identity": {"modelId": model_id,
                                        "marketingName": "x",
                                        "knowledgeCutoff": "May 2026"}}}


def _usage(inp=0, read=0, create=0, out=0):
    return {"input_tokens": inp, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": create, "output_tokens": out}


def _write_session(projects_dir, dir_name, rows, age_seconds=0):
    """寫一個假的 session 逐字稿，並把 mtime 設成 age_seconds 秒前。"""
    d = projects_dir / dir_name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "fake-session.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    t = time.time() - age_seconds
    os.utime(p, (t, t))
    return p


# --- token 算法 -----------------------------------------------------------

def test_context_佔用是最後一則的三個輸入欄位相加():
    """context 佔用＝這一輪送進模型的總長度，不是整個 session 的累計用量。
    累計用量已經有成本區塊在做了，兩者意義不同不可混用。"""
    assert session_context.context_tokens(
        _usage(inp=2, read=228988, create=577, out=845)) == 229567


def test_output_tokens_不算進_context():
    """output 是這一輪吐出來的，要到下一輪才會進 context。現在就加會高估。"""
    assert session_context.context_tokens(_usage(inp=0, read=0, create=0, out=5000)) == 0


def test_取最後一則而不是最大的一則(tmp_path):
    """compaction 之後 context 會掉下來，那是真的掉了。
    取最大值會讓 widget 永遠停在壓縮前的高點，等於顯示過期數字。"""
    p = tmp_path / "projects"
    _write_session(p, "-home-u-proj", [
        _model_attachment("claude-opus-5[1m]"),
        _assistant(_usage(read=900000)),
        _assistant(_usage(read=12000)),
    ])
    out = session_context.active_sessions(p)
    assert out[0]["tokens"] == 12000


def test_subagent_的行不算(tmp_path):
    """isSidechain 是子代理自己的 context，混進來主線數字會亂跳。"""
    p = tmp_path / "projects"
    _write_session(p, "-home-u-proj", [
        _model_attachment("claude-opus-5[1m]"),
        _assistant(_usage(read=50000)),
        _assistant(_usage(read=777777), sidechain=True),
    ])
    out = session_context.active_sessions(p)
    assert out[0]["tokens"] == 50000


# --- context window 的分母 ------------------------------------------------

def test_modelId_帶_1m_就是一百萬(tmp_path):
    p = tmp_path / "projects"
    _write_session(p, "-home-u-proj", [
        _model_attachment("claude-opus-5[1m]"),
        _assistant(_usage(read=100000)),
    ])
    out = session_context.active_sessions(p)
    assert out[0]["context_window"] == 1_000_000
    assert out[0]["percent"] == 10.0


def test_modelId_沒有_1m_就是二十萬(tmp_path):
    p = tmp_path / "projects"
    _write_session(p, "-home-u-proj", [
        _model_attachment("claude-sonnet-5"),
        _assistant(_usage(read=100000)),
    ])
    out = session_context.active_sessions(p)
    assert out[0]["context_window"] == 200_000
    assert out[0]["percent"] == 50.0


def test_找不到_modelId_不准猜分母(tmp_path):
    """🔴 這條是假數字防線。預設 20 萬會讓 1M 的 session 算出 116% 這種鬼數字
    （2026-09-13 實測撞過）。查不到就誠實留白，不得頂替。"""
    p = tmp_path / "projects"
    _write_session(p, "-home-u-proj", [_assistant(_usage(read=230000))])
    out = session_context.active_sessions(p)
    assert out[0]["tokens"] == 230000
    assert out[0]["context_window"] is None
    assert out[0]["percent"] is None


def test_session_中途換模型要以最後一次為準(tmp_path):
    """/model 換過之後分母跟著變。只讀開頭那一筆會算錯。"""
    p = tmp_path / "projects"
    _write_session(p, "-home-u-proj", [
        _model_attachment("claude-opus-5[1m]"),
        _assistant(_usage(read=100000)),
        _model_attachment("claude-sonnet-5"),
        _assistant(_usage(read=100000)),
    ])
    out = session_context.active_sessions(p)
    assert out[0]["context_window"] == 200_000


# --- 選哪些 session（Frank 2026-09-13 拍板：方案 B）-----------------------

def test_只列活動時間在窗口內的(tmp_path):
    """widget 要回答的是「我現在這個 session 用掉多少」，
    半小時前關掉的 session 留在畫面上只會誤導。"""
    p = tmp_path / "projects"
    _write_session(p, "-home-lintzuyang-Claude-newproj", [
        _model_attachment("claude-opus-5[1m]"), _assistant(_usage(read=1000))],
        age_seconds=10)
    _write_session(p, "-home-lintzuyang-Claude-oldproj", [
        _model_attachment("claude-opus-5[1m]"), _assistant(_usage(read=2000))],
        age_seconds=3600)
    out = session_context.active_sessions(p, window_minutes=5)
    assert [s["project"] for s in out] == ["newproj"]


def test_多開時依最近活動由新到舊排序(tmp_path):
    p = tmp_path / "projects"
    _write_session(p, "-home-lintzuyang-Claude-alpha", [
        _model_attachment("claude-opus-5[1m]"), _assistant(_usage(read=1000))],
        age_seconds=120)
    _write_session(p, "-home-lintzuyang-Claude-beta", [
        _model_attachment("claude-opus-5[1m]"), _assistant(_usage(read=2000))],
        age_seconds=5)
    out = session_context.active_sessions(p, window_minutes=5)
    assert [s["project"] for s in out] == ["beta", "alpha"]


def test_最多只列_limit_條(tmp_path):
    """畫面高度有限。多開五個不該把其他區塊擠掉。"""
    p = tmp_path / "projects"
    for i in range(5):
        _write_session(p, f"-home-u-p{i}", [
            _model_attachment("claude-opus-5[1m]"),
            _assistant(_usage(read=1000 * (i + 1)))], age_seconds=i)
    out = session_context.active_sessions(p, window_minutes=5, limit=3)
    assert len(out) == 3


def test_沒有任何_usage_的檔不列入(tmp_path):
    """剛開還沒對話的 session 沒有數字可顯示，列出來是一列空白。"""
    p = tmp_path / "projects"
    _write_session(p, "-home-u-empty", [_model_attachment("claude-opus-5[1m]")])
    assert session_context.active_sessions(p, window_minutes=5) == []


@pytest.mark.parametrize("dir_name", [
    "-home-lintzuyang-Claude-linux-claude-usage",
    "-home-lintzuyang-freebuff-project-manga",
    "-home-lintzuyang",
])
def test_專案名反解必須與專案排行共用同一套(tmp_path, dir_name):
    """🔴 同一個 widget 上，D 區塊的專案名必須跟 C 區塊（專案排行）逐字一致。
    自己另寫一套反解，畫面上同一個專案會出現兩種名字（實測 12 個目錄有 8 個對不上）。
    ⇒ 直接呼叫 transcript_scan._decode_project_name，不得複製一份。"""
    p = tmp_path / "projects"
    _write_session(p, dir_name, [
        _model_attachment("claude-opus-5[1m]"), _assistant(_usage(read=1000))])
    out = session_context.active_sessions(p)
    assert out[0]["project"] == transcript_scan._decode_project_name(dir_name)


def test_目錄不存在時回空陣列而不是炸掉(tmp_path):
    """SPEC §4.2：失敗要是可讀的降級。"""
    assert session_context.active_sessions(tmp_path / "不存在") == []


def test_last_active_at_是台灣時間(tmp_path):
    """SPEC §4.1：所有時間欄位進 state 之前就換算完，desklet 不做時區換算。"""
    p = tmp_path / "projects"
    _write_session(p, "-home-u-proj", [
        _model_attachment("claude-opus-5[1m]"), _assistant(_usage(read=1000))])
    out = session_context.active_sessions(p)
    assert out[0]["last_active_at"].endswith("+08:00")


# --- 效能硬約束（SPEC §3）------------------------------------------------

def test_窗口外的檔案不得被開啟(tmp_path, monkeypatch):
    """🔴 SPEC §3：禁止全量掃描。2012 iMac 上開 300 多個檔會卡住桌面。
    窗口外的檔連開都不該開，不是開了再丟掉。"""
    p = tmp_path / "projects"
    _write_session(p, "-home-u-new", [
        _model_attachment("claude-opus-5[1m]"), _assistant(_usage(read=1000))],
        age_seconds=10)
    old = _write_session(p, "-home-u-old", [
        _model_attachment("claude-opus-5[1m]"), _assistant(_usage(read=2000))],
        age_seconds=7200)

    opened = []
    real_open = builtins.open

    def spy(file, *a, **kw):
        opened.append(str(file))
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(io, "open", spy)
    session_context.active_sessions(p, window_minutes=5)
    assert str(old) not in opened


# --- 接進 state.json ------------------------------------------------------

def test_state_一定有_sessions_欄位():
    """SPEC §4.2：ok:false 時其餘欄位仍須存在，desklet 不得因缺欄位而炸掉。"""
    state = main.build_state(None, "API 掛了", None, "掃描掛了")
    assert state["sessions"] == []


def test_state_原樣帶出_sessions():
    """collector 算好就好，desklet 只負責畫。"""
    rows = [{"project": "quantum", "tokens": 123, "context_window": 1_000_000,
             "percent": 0.0, "model": "claude-opus-5[1m]",
             "last_active_at": "2026-09-13T01:00:00+08:00"}]
    state = main.build_state(None, None, None, None, sessions=rows)
    assert state["sessions"] == rows
