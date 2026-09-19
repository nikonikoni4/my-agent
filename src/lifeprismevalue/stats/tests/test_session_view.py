"""stats/session_view 读回边界的形态归一化测试。

session 文件里 `tool/call` 的 arguments 有两种落盘形态：provider 解析成功时是嵌套
对象，未解析成功时是 wire 原样字符串。session_view 是**绕过 Session 类直接逐行读
文件**的（报表/统计走这条路），所以归一化得在它自己的读回边界做——ToolCallView
的既有契约（arguments 是字符串、要结构化用 arguments_json）才继续成立，统计产物
的格式也不跟着落盘形态漂移。
"""

from __future__ import annotations

import json

from lifeprismevalue.stats.session_view import load_session_view


def _write_session(path, calls) -> None:
    """落一个最小可读的 session 文件：每条 call 一行 tool/call。"""
    lines = [
        json.dumps(
            {
                "type": "tool/call",
                "seq": index,
                "turn": 1,
                "step": 1,
                "timestamp": "2026-09-19T10:00:00+08:00",
                "data": {"call_id": call_id, "tool_name": "read_file", "arguments": arguments},
            },
            ensure_ascii=False,
        )
        for index, (call_id, arguments) in enumerate(calls, start=1)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_落盘是嵌套对象时读回归一化为字符串(tmp_path):
    """解析成功的调用落盘是 dict；读回后 arguments 是字符串，结构化交给 arguments_json。"""
    path = tmp_path / "s1.jsonl"
    _write_session(path, [("c1", {"file_path": "D:/data/user/a.md", "offset": 1})])

    call = load_session_view(path).tool_calls[0]
    assert isinstance(call.arguments, str), "读回边界应把嵌套对象归一化成字符串"
    assert json.loads(call.arguments) == {"file_path": "D:/data/user/a.md", "offset": 1}
    assert call.arguments_json == {"file_path": "D:/data/user/a.md", "offset": 1}


def test_落盘是原文时原样保留(tmp_path):
    """解析失败的调用落盘就是坏 JSON 原文：不能丢，也不能被当成 JSON 解析。"""
    path = tmp_path / "s2.jsonl"
    raw = '{"file_path": "坏 JSON'
    _write_session(path, [("c1", raw)])

    call = load_session_view(path).tool_calls[0]
    assert call.arguments == raw
    assert call.arguments_json is None


def test_归一化时中文不被转义(tmp_path):
    """归一化要用 ensure_ascii=False：默认的 True 会把中文转成 \\uXXXX，
    统计产物里就再也读不回中文了。"""
    path = tmp_path / "s3.jsonl"
    _write_session(path, [("c1", {"city": "北京"})])

    call = load_session_view(path).tool_calls[0]
    assert "北京" in call.arguments
