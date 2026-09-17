"""只读工具的寄存器驱动测试（基于隔离的快照库，数据稳定无副作用）。"""

from __future__ import annotations

import json

import pytest
from myagent.agent.core.provider import RawToolCall
from myagent.agent.core.tool.register import ToolRegister
from myagent.agent.core.tool.tool import ToolResult

from lifeprismevalue.tools import build_lifeprism_tools


def call(name: str, args: dict) -> RawToolCall:
    return RawToolCall(id="t1", name=name, arguments=json.dumps(args, ensure_ascii=False))


def make_register() -> ToolRegister:
    register = ToolRegister()
    register.register(build_lifeprism_tools())
    return register


def _json(r: ToolResult):
    assert not r.is_error, r.content
    return json.loads(r.content[len("Success: "):])


@pytest.mark.asyncio
async def test_query_user_activity_summary() -> None:
    register = make_register()
    r = await register.execute(
        call(
            "query_user_activity_summary",
            {
                "query_option": ["high_usage_segments", "computer_overview",
                                 "user_behavior_notes", "ai_behavior_notes", "todolist"],
                "start_time": "2026-04-28 00:00:00",
                "end_time": "2026-04-29 00:00:00",
            },
        )
    )
    assert not r.is_error, r.content
    for section in ("电脑高频使用时段分析", "电脑总体使用统计",
                    "用户自定义行为备注", "AI分析行为备注", "用户待办事项"):
        assert section in r.content


@pytest.mark.asyncio
async def test_query_user_activity_log() -> None:
    register = make_register()
    r = await register.execute(
        call(
            "query_user_activity_log",
            {"start_time": "2026-04-28 00:00:00", "end_time": "2026-04-29 00:00:00",
             "duration_min": 60},
        )
    )
    assert not r.is_error, r.content
    assert "日志查询说明" in r.content


@pytest.mark.asyncio
async def test_query_user_mood() -> None:
    register = make_register()
    r = await register.execute(
        call(
            "query_user_mood",
            {"start_time": "2026-05-01 00:00:00", "end_time": "2026-05-31 00:00:00"},
        )
    )
    assert not r.is_error, r.content
    assert "心情" in r.content


@pytest.mark.asyncio
async def test_query_user_habits() -> None:
    register = make_register()
    r = await register.execute(call("query_user_habits", {}))
    assert not r.is_error, r.content
    assert r.content.startswith("Success:")
    assert "个习惯" in r.content


@pytest.mark.asyncio
async def test_list_custom_record_types() -> None:
    register = make_register()
    payload = _json(await register.execute(call("list_custom_record_types", {})))
    assert isinstance(payload, list) and payload
    assert "fields" in payload[0]