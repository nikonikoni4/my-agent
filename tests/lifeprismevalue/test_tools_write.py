"""写工具的寄存器驱动测试（在隔离副本库上执行，用后清理，保持快照数据干净）。"""

from __future__ import annotations

import json
import uuid

import pytest
from myagent.agent.core.provider import RawToolCall
from myagent.agent.core.tool.register import ToolRegister
from myagent.agent.core.tool.tool import ToolErrorType, ToolResult

from lifeprismevalue import db
from lifeprismevalue.tools import build_lifeprism_tools
from lifeprismevalue.tools.base import SUCCESS

_MARK = f"lwvtest-{uuid.uuid4().hex[:8]}"


def call(name: str, args: dict) -> RawToolCall:
    return RawToolCall(id="t1", name=name, arguments=json.dumps(args, ensure_ascii=False))


def make_register() -> ToolRegister:
    register = ToolRegister()
    register.register(build_lifeprism_tools())
    return register


def _json_tail(r: ToolResult):
    assert not r.is_error, r.content
    return json.loads(r.content[len(SUCCESS):])


def _cleanup_custom_type(slug: str) -> None:
    row = db.query_one("SELECT id FROM custom_record_types WHERE slug = ?", (slug,))
    if not row:
        return
    db.execute(f"DROP TABLE IF EXISTS custom_{slug}")
    db.execute("DELETE FROM custom_record_fields WHERE type_id = ?", (row["id"],))
    db.execute("DELETE FROM custom_record_types WHERE id = ?", (row["id"],))


@pytest.mark.asyncio
async def test_create_or_update_user_behavior_note() -> None:
    register = make_register()
    content = f"{_MARK} 备注"
    r = await register.execute(
        call(
            "create_or_update_user_behavior_note",
            {"start_time": "2026-09-12 09:00:00", "end_time": "2026-09-12 09:30:00",
             "content": content},
        )
    )
    assert not r.is_error, r.content
    assert "创建行为备注成功" in r.content
    block_id = int(r.content.split("ID: ")[1].split(")")[0])

    # 更新
    r2 = await register.execute(
        call(
            "create_or_update_user_behavior_note",
            {"start_time": "2026-09-12 09:00:00", "end_time": "2026-09-12 10:00:00",
             "content": content + "-2", "block_id": block_id},
        )
    )
    assert not r2.is_error, r2.content
    assert "更新行为备注成功" in r2.content

    # 清理
    db.execute("DELETE FROM timeline_custom_block WHERE id = ?", (block_id,))


@pytest.mark.asyncio
async def test_create_user_mood() -> None:
    register = make_register()
    content = f"{_MARK} 心情"
    r = await register.execute(
        call("create_user_mood", {"content": content, "mood_type_id": "joy"})
    )
    assert not r.is_error, r.content
    assert "创建心情记录成功" in r.content

    # 清理
    db.execute("DELETE FROM mood_entries WHERE content = ?", (content,))


@pytest.mark.asyncio
async def test_custom_record_flow() -> None:
    register = make_register()
    slug = "test_run_" + _MARK[:6]

    # 1. 建类型
    r = await register.execute(
        call(
            "create_custom_record_type",
            {"name": "测试跑步", "slug": slug,
             "fields": [{"field_name": "距离(km)", "field_key": "distance", "field_type": "float"}]},
        )
    )
    assert not r.is_error, r.content
    type_id = json.loads(r.content[r.content.index("{"):])["type_id"]

    try:
        # 2. 录入（字符串自动转 float）
        r = await register.execute(
            call(
                "create_custom_record_entry",
                {"type_id": type_id, "data": {"distance": "5.5"},
                 "event_time": "2026-09-10 07:00:00"},
            )
        )
        assert not r.is_error, r.content

        # 3. 查询
        r = await register.execute(
            call("query_custom_record_entries", {"type_id": type_id, "limit": 5})
        )
        payload = _json_tail(r)
        assert payload["total"] == 1
        assert payload["entries"][0]["distance"] == 5.5
    finally:
        _cleanup_custom_type(slug)


@pytest.mark.asyncio
async def test_custom_record_bad_field_value_is_error() -> None:
    """字段值类型不匹配应返回结构化错误，且被归类为 TOOL_EXECUTION。"""
    register = make_register()
    slug = "test_run_bad_" + _MARK[:4]
    await register.execute(
        call(
            "create_custom_record_type",
            {"name": "测试", "slug": slug,
             "fields": [{"field_name": "距离(km)", "field_key": "distance", "field_type": "float"}]},
        )
    )
    try:
        row = db.query_one("SELECT id FROM custom_record_types WHERE slug = ?", (slug,))
        r = await register.execute(
            call("create_custom_record_entry", {"type_id": row["id"], "data": {"distance": "abc"}})
        )
        assert r.is_error
        assert r.error_type is ToolErrorType.TOOL_EXECUTION
        assert "INVALID_FIELD_VALUE" in r.content
    finally:
        _cleanup_custom_type(slug)