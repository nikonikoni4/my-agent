"""习惯行为逻辑：在隔离副本库上构造覆盖"今天"的习惯与进行中挑战，端到端验证打卡/取消/补签。"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from myagent.agent.core.provider import RawToolCall
from myagent.agent.core.tool.register import ToolRegister

from lifeprismevalue import db
from lifeprismevalue.tools import build_lifeprism_tools
from lifeprismevalue.utils.time_utils import get_local_today

from lifeprismevalue.utils.time_utils import get_utc_now_iso


def call(name: str, args: dict) -> RawToolCall:
    return RawToolCall(id="t1", name=name, arguments=json.dumps(args, ensure_ascii=False))


def make_register() -> ToolRegister:
    register = ToolRegister()
    register.register(build_lifeprism_tools())
    return register


@pytest.fixture()
def live_habit() -> str:
    """构造一个覆盖"今天"的活跃习惯 + 进行中挑战，返回 habit_id（用后清理）。"""
    habit_id = f"habit-lwvtest-{uuid.uuid4().hex[:6]}"
    today = get_local_today()
    now_utc = get_utc_now_iso()
    db.execute(
        "INSERT INTO habits (id, name, frequency_type, current_level, status, created_at, updated_at) "
        "VALUES (?, ?, 'daily', 0, 'active', ?, ?)",
        (habit_id, "测试习惯", now_utc, now_utc),
    )
    challenge_id = f"challenge-{uuid.uuid4().hex[:6]}"
    db.execute(
        "INSERT INTO habit_challenges "
        "(id, habit_id, challenge_weeks, required_completions, from_level, to_level, "
        " start_date, end_date, completed_count, streak_base, status, created_at, updated_at) "
        "VALUES (?, ?, 2, 10, 0, 1, ?, ?, 0, 0, 'in_progress', ?, ?)",
        (challenge_id, habit_id, (today - timedelta(days=1)).isoformat(),
         (today + timedelta(days=7)).isoformat(), now_utc, now_utc),
    )
    yield habit_id
    db.execute("DELETE FROM habit_checkins WHERE habit_id = ?", (habit_id,))
    db.execute("DELETE FROM habit_challenges WHERE habit_id = ?", (habit_id,))
    db.execute("DELETE FROM habits WHERE id = ?", (habit_id,))


@pytest.mark.asyncio
async def test_checkin_then_cancel(live_habit: str) -> None:
    register = make_register()

    r = await register.execute(call("checkin_habit", {"habit_id": live_habit}))
    assert not r.is_error, r.content
    assert "打卡成功" in r.content
    assert db.query_one("SELECT * FROM habit_checkins WHERE habit_id = ?", (live_habit,)) is not None

    r = await register.execute(call("cancel_checkin_habit", {"habit_id": live_habit}))
    assert not r.is_error, r.content
    assert "已取消今日打卡" in r.content
    assert db.query_one("SELECT * FROM habit_checkins WHERE habit_id = ?", (live_habit,)) is None


@pytest.mark.asyncio
async def test_backfill_in_window(live_habit: str) -> None:
    register = make_register()
    today = get_local_today()
    backfill_date = (today - timedelta(days=1)).isoformat()

    r = await register.execute(
        call("backfill_checkin", {"habit_id": live_habit, "dates": [backfill_date]})
    )
    assert not r.is_error, r.content
    assert "补签完成" in r.content
    assert "补签成功" in r.content
    assert db.query_one(
        "SELECT * FROM habit_checkins WHERE habit_id = ? AND date = ?", (live_habit, backfill_date)
    ) is not None


@pytest.mark.asyncio
async def test_backfill_out_of_window_fails(live_habit: str) -> None:
    register = make_register()
    today = get_local_today()
    too_early = (today - timedelta(days=2)).isoformat()

    r = await register.execute(
        call("backfill_checkin", {"habit_id": live_habit, "dates": [too_early]})
    )
    assert not r.is_error, r.content  # 补签把单个失败项作为结果显示，不算工具级错误
    assert "补签完成" in r.content
    assert "失败" in r.content
    assert db.query_one(
        "SELECT * FROM habit_checkins WHERE habit_id = ? AND date = ?", (live_habit, too_early)
    ) is None


@pytest.mark.asyncio
async def test_query_user_habits_after_insert(live_habit: str) -> None:
    register = make_register()
    r = await register.execute(call("query_user_habits", {}))
    assert not r.is_error, r.content
    assert "测试习惯" in r.content