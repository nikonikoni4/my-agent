"""习惯数据访问层 + 核心业务逻辑

移植自 lifeprism 的 habit_providers / habit_service / habit_stats_service，
去掉 pydantic 依赖，用 dataclass 表达返回对象。

涉及表：habits / habit_challenges / habit_checkins
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from lifeprismevalue import db
from lifeprismevalue.utils.time_utils import get_local_today, get_utc_now_iso

# ---------- 异常 ----------


class NotFoundError(Exception):
    def __init__(self, message: str, code: str | None = None):
        self.message = message
        self.code = code
        super().__init__(message)


class ConflictError(Exception):
    def __init__(self, message: str, code: str | None = None):
        self.message = message
        self.code = code
        super().__init__(message)


class ValidationError(Exception):
    def __init__(self, message: str, code: str | None = None):
        self.message = message
        self.code = code
        super().__init__(message)


# ---------- 返回对象（dataclass） ----------


@dataclass
class FrequencyObject:
    type: str
    specific_days: list[int] | None = None


@dataclass
class ChallengeObject:
    id: str
    habit_id: str
    from_level: int
    to_level: int
    challenge_weeks: int
    required_completions: int
    completed_count: int
    remaining_rest_days: int
    start_date: str
    end_date: str
    streak_base: int
    status: str
    finished_at: str | None = None


@dataclass
class HabitListItem:
    id: str
    name: str
    description: str | None
    frequency: FrequencyObject
    current_level: int
    status: str
    current_challenge: ChallengeObject | None
    value_id: str | None
    commitment_id: str | None
    created_at: str
    paused_at: str | None
    streak: int
    anchor_info: Any = None
    today_completed: bool = False


@dataclass
class CheckInObject:
    id: str
    habit_id: str
    challenge_id: str
    date: str
    completed: bool
    completed_at: str
    created_at: str


@dataclass
class SettlementItem:
    challenge_id: str
    habit_id: str
    habit_name: str
    result: str  # "succeeded" | "failed"
    from_level: int
    to_level: int
    completed_count: int
    required_completions: int
    can_save_by_backfill: bool


@dataclass
class CheckInResponse:
    checkin: CheckInObject
    habit: HabitListItem
    settlement: SettlementItem | None = None


@dataclass
class CancelCheckInResponse:
    habit: HabitListItem
    settlement: SettlementItem | None = None


@dataclass
class BackfillCheckInItem:
    date: str


@dataclass
class BackfillCheckInRequest:
    challenge_id: str
    items: list[BackfillCheckInItem]


@dataclass
class BackfillCheckInResultItem:
    date: str
    status: str
    checkin: Any
    settlement: Any
    error_code: str | None
    message: str | None


@dataclass
class BackfillCheckInBatchSummary:
    total: int
    succeeded: int
    failed: int


@dataclass
class BackfillCheckInBatchResponse:
    habit: HabitListItem
    results: list[BackfillCheckInResultItem]
    summary: BackfillCheckInBatchSummary


@dataclass
class HabitListResponse:
    habits: list[HabitListItem] = field(default_factory=list)


@dataclass
class HabitDetailResponse:
    id: str
    name: str
    description: str | None
    frequency: FrequencyObject
    current_level: int
    status: str
    current_challenge: ChallengeObject | None
    value_id: str | None
    commitment_id: str | None
    created_at: str
    paused_at: str | None
    streak: int
    anchor_info: Any = None
    today_completed: bool = False


# ---------- 错误码 ----------

HABIT_NOT_FOUND = "HABIT_NOT_FOUND"
HABIT_NOT_ACTIVE = "HABIT_NOT_ACTIVE"
CHALLENGE_NOT_FOUND = "CHALLENGE_NOT_FOUND"
CHECKIN_ALREADY_EXISTS = "CHECKIN_ALREADY_EXISTS"
CHECKIN_NOT_FOUND = "CHECKIN_NOT_FOUND"
CANNOT_CANCEL_PAST_CHECKIN = "CANNOT_CANCEL_PAST_CHECKIN"
BACKFILL_DATE_OUT_OF_WINDOW = "BACKFILL_DATE_OUT_OF_WINDOW"
VALIDATION_FAILED = "VALIDATION_FAILED"
INVALID_STATUS_TRANSITION = "INVALID_STATUS_TRANSITION"

# ---------- 常量 ----------

LEVEL_CHALLENGE_WEEKS = {0: 2, 1: 3, 2: 4, 3: 8, 4: 12}
MAX_LEVEL = 4


def _generate_id(prefix: str) -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}"


# ---------- Repository 层 ----------


def _parse_frequency(row: dict) -> FrequencyObject:
    config = None
    if row.get("frequency_config"):
        try:
            config = json.loads(row["frequency_config"])
        except (json.JSONDecodeError, TypeError) as e:
            raise ValidationError(f"习惯频率配置损坏: {e}", code=VALIDATION_FAILED) from e
    specific_days = config.get("specificDays") if config else None
    return FrequencyObject(type=row["frequency_type"], specific_days=specific_days)


def get_habits(status: str | None = None) -> list[dict[str, Any]]:
    """获取习惯列表，可按 status 过滤，按 created_at 升序。"""
    if status:
        return db.query_all(
            "SELECT * FROM habits WHERE status = ? ORDER BY created_at ASC", (status,)
        )
    return db.query_all("SELECT * FROM habits ORDER BY created_at ASC")


def get_habit_by_id(habit_id: str) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM habits WHERE id = ?", (habit_id,))


def get_current_challenge(habit_id: str) -> dict[str, Any] | None:
    """获取习惯当前进行中的挑战（status = 'in_progress'）。"""
    return db.query_one(
        "SELECT * FROM habit_challenges WHERE habit_id = ? AND status = 'in_progress' "
        "ORDER BY id LIMIT 1",
        (habit_id,),
    )


def get_challenge_by_id(challenge_id: str) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM habit_challenges WHERE id = ?", (challenge_id,))


def get_checkin_by_date(habit_id: str, checkin_date: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM habit_checkins WHERE habit_id = ? AND date = ?", (habit_id, checkin_date)
    )


def get_checkin_dates_by_challenge(habit_id: str, challenge_id: str) -> list[str]:
    rows = db.query_all(
        "SELECT date FROM habit_checkins WHERE habit_id = ? AND challenge_id = ? "
        "ORDER BY date ASC",
        (habit_id, challenge_id),
    )
    return [r["date"] for r in rows]


def create_challenge(data: dict[str, Any]) -> str:
    challenge_id = _generate_id("challenge")
    insert_data = {
        "id": challenge_id,
        "habit_id": data["habit_id"],
        "challenge_weeks": data["challenge_weeks"],
        "required_completions": data["required_completions"],
        "from_level": data["from_level"],
        "to_level": data["to_level"],
        "start_date": data["start_date"],
        "end_date": data["end_date"],
        "completed_count": data.get("completed_count", 0),
        "streak_base": data.get("streak_base", 0),
        "status": data.get("status", "in_progress"),
        "finished_at": data.get("finished_at"),
        "created_at": get_utc_now_iso(),
        "updated_at": get_utc_now_iso(),
    }
    cols = ", ".join(insert_data.keys())
    ph = ", ".join("?" * len(insert_data))
    db.execute(
        f"INSERT INTO habit_challenges ({cols}) VALUES ({ph})", list(insert_data.values())
    )
    return challenge_id


_CHALLENGE_UPDATE_FIELDS = {
    "completed_count",
    "status",
    "finished_at",
    "start_date",
    "end_date",
    "required_completions",
    "challenge_weeks",
    "from_level",
    "to_level",
    "streak_base",
}


def update_challenge(challenge_id: str, update_data: dict[str, Any]) -> bool:
    if not update_data:
        return True
    update_data = {k: v for k, v in update_data.items() if k in _CHALLENGE_UPDATE_FIELDS}
    if not update_data:
        return True
    update_data["updated_at"] = get_utc_now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in update_data)
    affected = db.execute(
        f"UPDATE habit_challenges SET {set_clause} WHERE id = ?",
        list(update_data.values()) + [challenge_id],
    )
    return affected > 0


def create_checkin(data: dict[str, Any]) -> str | None:
    """创建打卡记录。若 UNIQUE(habit_id, date) 冲突返回 None。"""
    checkin_id = _generate_id("checkin")
    now_str = get_utc_now_iso()
    insert_data = {
        "id": checkin_id,
        "habit_id": data["habit_id"],
        "challenge_id": data["challenge_id"],
        "date": data["date"],
        "completed_at": data.get("completed_at", now_str),
        "created_at": now_str,
        "updated_at": now_str,
    }
    cols = ", ".join(insert_data.keys())
    ph = ", ".join("?" * len(insert_data))
    cursor_rowcount = db.execute(
        f"INSERT OR IGNORE INTO habit_checkins ({cols}) VALUES ({ph})",
        list(insert_data.values()),
    )
    if cursor_rowcount == 0:
        return None
    return checkin_id


def delete_checkin(habit_id: str, checkin_date: str) -> bool:
    affected = db.execute(
        "DELETE FROM habit_checkins WHERE habit_id = ? AND date = ?", (habit_id, checkin_date)
    )
    return affected > 0


# ---------- 频率 / Streak 计算 ----------


def is_scheduled_day(d: date, freq: FrequencyObject) -> bool:
    if freq.type == "daily":
        return True
    elif freq.type == "weekdays":
        return d.weekday() < 5
    elif freq.type == "weekend":
        return d.weekday() >= 5
    elif freq.type == "custom":
        return (d.weekday() + 1) in (freq.specific_days or [])
    return True


def count_scheduled_days_in_range(start: date, end: date, freq: FrequencyObject) -> int:
    count = 0
    current = start
    while current <= end:
        if is_scheduled_day(current, freq):
            count += 1
        current += timedelta(days=1)
    return count


def _get_daily_anchor_date(checkin_dates: set, challenge_start: date, today: date) -> date | None:
    anchor = today if today.isoformat() in checkin_dates else today - timedelta(days=1)
    if anchor < challenge_start:
        return None
    return anchor


def _has_daily_gap_between_challenge_start_and_anchor(
    checkin_dates: set, challenge_start: date, anchor: date
) -> bool:
    current = challenge_start
    while current <= anchor:
        if current.isoformat() not in checkin_dates:
            return True
        current += timedelta(days=1)
    return False


def calculate_daily_streak(checkin_dates: set, challenge: dict, today: date) -> int:
    challenge_start = date.fromisoformat(challenge["start_date"])
    anchor = _get_daily_anchor_date(checkin_dates, challenge_start, today)
    if anchor is None:
        return 0
    streak = 0
    current = anchor
    while current >= challenge_start:
        if current.isoformat() in checkin_dates:
            streak += 1
        else:
            break
        current -= timedelta(days=1)
    return streak


def calculate_weekly_streak(checkin_dates: set, challenge: dict, freq: FrequencyObject, today: date) -> int:
    challenge_start = date.fromisoformat(challenge["start_date"])
    challenge_end = date.fromisoformat(challenge["end_date"])
    timeline_end = min(today, challenge_end)
    if timeline_end < challenge_start:
        return 0

    checkin_day_set = {
        date.fromisoformat(d)
        for d in checkin_dates
        if challenge_start <= date.fromisoformat(d) <= timeline_end
    }
    streak = challenge.get("streak_base") or 0
    current = challenge_start
    while current <= timeline_end:
        if current.weekday() == 0:
            prev_week_start = current - timedelta(days=7)
            prev_week_end = current - timedelta(days=1)
            eff_start = max(prev_week_start, challenge_start)
            eff_end = min(prev_week_end, challenge_end)
            if eff_end >= eff_start:
                scheduled = count_scheduled_days_in_range(eff_start, eff_end, freq)
                if scheduled > 0:
                    completed = sum(
                        1
                        for i in range((eff_end - eff_start).days + 1)
                        if (eff_start + timedelta(days=i)) in checkin_day_set
                    )
                    if completed < scheduled:
                        streak = 0
        if current in checkin_day_set:
            streak += 1
        current += timedelta(days=1)
    return streak


def get_habit_streak(habit_id: str, freq: FrequencyObject, challenge: dict | None) -> int:
    """获取习惯当前 Streak（含 streak_base）。"""
    if not challenge:
        return 0
    today = date.today()
    checkin_list = get_checkin_dates_by_challenge(habit_id, challenge["id"])
    checkin_set = set(checkin_list)
    streak_base = challenge.get("streak_base") or 0
    if freq.type == "daily":
        challenge_start = date.fromisoformat(challenge["start_date"])
        anchor = _get_daily_anchor_date(checkin_set, challenge_start, today)
        current = calculate_daily_streak(checkin_set, challenge, today)
        if current <= 0:
            return 0
        if anchor is not None and _has_daily_gap_between_challenge_start_and_anchor(
            checkin_set, challenge_start, anchor
        ):
            return current
        return streak_base + current
    else:
        return calculate_weekly_streak(checkin_set, challenge, freq, today)


# ---------- 业务逻辑 ----------


def get_weekly_frequency_days(freq: FrequencyObject) -> int:
    if freq.type == "daily":
        return 7
    elif freq.type == "weekdays":
        return 5
    elif freq.type == "weekend":
        return 2
    elif freq.type == "custom":
        return len(freq.specific_days) if freq.specific_days else 0
    return 0


def calculate_challenge_params(level: int, freq: FrequencyObject) -> dict:
    weeks = LEVEL_CHALLENGE_WEEKS.get(level, 2)
    weekly_days = get_weekly_frequency_days(freq)
    total_expected = weeks * weekly_days
    required = math.ceil(total_expected * 0.85)
    return {"challengeWeeks": weeks, "requiredCompletions": required}


class HabitService:
    """习惯系统核心业务逻辑（有状态单例）。"""

    def __init__(self):
        self._habit_name_map: dict[str, str] = {}
        self._refresh_cache()

    def _refresh_cache(self):
        habits = get_habits()
        self._habit_name_map = {h["id"]: h["name"] for h in habits}

    # ---- 内部组装 ----

    def _calculate_remaining_rest_days(self, habit_id: str, challenge: dict) -> int:
        if challenge["status"] != "in_progress":
            return 0
        remaining_checkin_days = self._get_remaining_checkin_days(
            habit_id, challenge, get_local_today()
        )
        return max(
            0,
            challenge["completed_count"]
            + remaining_checkin_days
            - challenge["required_completions"],
        )

    def _build_challenge_object(self, c: dict | None, habit_id: str) -> ChallengeObject | None:
        if not c:
            return None
        remaining_rest_days = self._calculate_remaining_rest_days(habit_id, c)
        return ChallengeObject(
            id=c["id"],
            habit_id=c["habit_id"],
            from_level=c["from_level"],
            to_level=c["to_level"],
            challenge_weeks=c["challenge_weeks"],
            required_completions=c["required_completions"],
            completed_count=c["completed_count"],
            remaining_rest_days=remaining_rest_days,
            start_date=c["start_date"],
            end_date=c["end_date"],
            streak_base=c["streak_base"],
            status=c["status"],
            finished_at=c.get("finished_at"),
        )

    def _build_habit_response(self, row: dict) -> HabitListItem:
        freq = _parse_frequency(row)
        challenge_row = get_current_challenge(row["id"])
        challenge_obj = self._build_challenge_object(challenge_row, row["id"])
        streak = get_habit_streak(row["id"], freq, challenge_row)
        today_str = get_local_today().isoformat()
        today_checkin = get_checkin_by_date(row["id"], today_str)

        return HabitListItem(
            id=row["id"],
            name=row["name"],
            description=row.get("description"),
            frequency=freq,
            current_level=row["current_level"],
            status=row["status"],
            current_challenge=challenge_obj,
            value_id=row.get("value_id"),
            commitment_id=row.get("commitment_id"),
            created_at=row["created_at"],
            paused_at=row.get("paused_at"),
            streak=streak,
            today_completed=bool(today_checkin),
        )

    def _create_challenge_for_habit(
        self,
        habit_id: str,
        level: int,
        freq: FrequencyObject,
        streak_base: int,
        previous_challenge: dict | None = None,
    ) -> dict:
        params = calculate_challenge_params(level, freq)
        today = get_local_today()

        if previous_challenge:
            old_end_date = date.fromisoformat(previous_challenge["end_date"])
            start_date = old_end_date + timedelta(days=1)
        else:
            start_date = today

        start = start_date.isoformat()
        end = (start_date + timedelta(weeks=params["challengeWeeks"])).isoformat()
        to_level = min(level + 1, MAX_LEVEL)
        data = {
            "habit_id": habit_id,
            "challenge_weeks": params["challengeWeeks"],
            "required_completions": params["requiredCompletions"],
            "from_level": level,
            "to_level": to_level,
            "start_date": start,
            "end_date": end,
            "completed_count": 0,
            "streak_base": streak_base,
            "status": "in_progress",
        }
        cid = create_challenge(data)
        return get_challenge_by_id(cid)

    def _cancel_current_challenge(self, habit_id: str):
        current = get_current_challenge(habit_id)
        if current:
            update_challenge(
                current["id"],
                {"status": "cancelled", "finished_at": get_utc_now_iso()},
            )

    def _get_remaining_checkin_days(self, habit_id: str, challenge: dict, today: date) -> int:
        end_date = date.fromisoformat(challenge["end_date"])
        if end_date < today:
            return 0
        remaining_future_days = (end_date - today).days
        today_checkin = get_checkin_by_date(habit_id, today.isoformat())
        return remaining_future_days + (0 if today_checkin else 1)

    def _can_save_by_backfill(
        self, habit_id: str, challenge: dict, completed: int, required: int
    ) -> bool:
        today = get_local_today()
        start_date = date.fromisoformat(challenge["start_date"])
        end_date = date.fromisoformat(challenge["end_date"])
        backfill_count = 0
        for i in range(1, 7):
            d = (today - timedelta(days=i)).isoformat()
            if date.fromisoformat(d) < start_date:
                break
            existing = get_checkin_by_date(habit_id, d)
            if not existing:
                backfill_count += 1
        remaining_future_days = max((end_date - today).days, 0)
        return (completed + backfill_count + remaining_future_days) >= required

    def _judge_challenge_result(
        self, habit_id: str, challenge_id: str, persist_succeeded: bool, persist_failed: bool
    ) -> SettlementItem | None:
        challenge = get_challenge_by_id(challenge_id)
        if not challenge or challenge["status"] != "in_progress":
            return None

        today = get_local_today()
        end_date = date.fromisoformat(challenge["end_date"])

        completed = challenge["completed_count"]
        required = challenge["required_completions"]
        habit_row = get_habit_by_id(habit_id)
        habit_name = habit_row["name"] if habit_row else ""
        reached_end_date = today >= end_date
        remaining_checkin_days = self._get_remaining_checkin_days(habit_id, challenge, today)

        if reached_end_date and completed >= required:
            new_level = min(challenge["to_level"], MAX_LEVEL)
            if persist_succeeded:
                update_challenge(
                    challenge["id"],
                    {"status": "succeeded", "finished_at": get_utc_now_iso()},
                )
                update_habit_level(habit_id, new_level)
                freq = _parse_frequency(habit_row)
                self._create_challenge_for_habit(habit_id, new_level, freq, completed, challenge)
            return SettlementItem(
                challenge_id=challenge["id"],
                habit_id=habit_id,
                habit_name=habit_name,
                result="succeeded",
                from_level=challenge["from_level"],
                to_level=new_level,
                completed_count=completed,
                required_completions=required,
                can_save_by_backfill=False,
            )

        if required > (completed + remaining_checkin_days):
            can_save = self._can_save_by_backfill(habit_id, challenge, completed, required)
            if persist_failed:
                update_challenge(
                    challenge["id"],
                    {"status": "failed", "finished_at": get_utc_now_iso()},
                )
            return SettlementItem(
                challenge_id=challenge["id"],
                habit_id=habit_id,
                habit_name=habit_name,
                result="failed",
                from_level=challenge["from_level"],
                to_level=challenge["from_level"],
                completed_count=completed,
                required_completions=required,
                can_save_by_backfill=can_save,
            )

        return None

    # ---- 工具使用的公开方法 ----

    def get_habits(self, status: str | None) -> HabitListResponse:
        rows = get_habits(status=status)
        items = [self._build_habit_response(r) for r in rows]
        return HabitListResponse(habits=items)

    def get_habit_detail(self, habit_id: str) -> HabitDetailResponse:
        row = get_habit_by_id(habit_id)
        if not row:
            raise NotFoundError("习惯不存在", code=HABIT_NOT_FOUND)
        item = self._build_habit_response(row)
        return HabitDetailResponse(
            id=item.id,
            name=item.name,
            description=item.description,
            frequency=item.frequency,
            current_level=item.current_level,
            status=item.status,
            current_challenge=item.current_challenge,
            value_id=item.value_id,
            commitment_id=item.commitment_id,
            created_at=item.created_at,
            paused_at=item.paused_at,
            streak=item.streak,
            today_completed=item.today_completed,
        )

    def checkin_today(self, habit_id: str) -> CheckInResponse:
        row = get_habit_by_id(habit_id)
        if not row:
            raise NotFoundError("习惯不存在", code=HABIT_NOT_FOUND)
        if row["status"] != "active":
            raise ValidationError("习惯处于暂停状态，无法打卡", code=HABIT_NOT_ACTIVE)

        challenge = get_current_challenge(habit_id)
        if not challenge:
            raise NotFoundError("当前无进行中的挑战", code=CHALLENGE_NOT_FOUND)

        today_str = get_local_today().isoformat()
        challenge_start = challenge["start_date"]
        if today_str < challenge_start:
            raise ValidationError(
                f"当前挑战从 {challenge_start} 开始，无法为 {today_str} 打卡",
                code=VALIDATION_FAILED,
            )

        now_str = get_utc_now_iso()
        checkin_id = create_checkin(
            {"habit_id": habit_id, "challenge_id": challenge["id"], "date": today_str}
        )
        if not checkin_id:
            raise ConflictError("今日已打卡，不可重复打卡", code=CHECKIN_ALREADY_EXISTS)

        new_count = challenge["completed_count"] + 1
        update_challenge(challenge["id"], {"completed_count": new_count})

        settlement = self._judge_challenge_result(habit_id, challenge["id"], True, False)

        checkin_obj = CheckInObject(
            id=checkin_id,
            habit_id=habit_id,
            challenge_id=challenge["id"],
            date=today_str,
            completed=True,
            completed_at=now_str,
            created_at=now_str,
        )
        habit_item = self._build_habit_response(get_habit_by_id(habit_id))
        return CheckInResponse(checkin=checkin_obj, habit=habit_item, settlement=settlement)

    def cancel_checkin(self, habit_id: str, date_str: str) -> CancelCheckInResponse:
        row = get_habit_by_id(habit_id)
        if not row:
            raise NotFoundError("习惯不存在", code=HABIT_NOT_FOUND)

        today_str = get_local_today().isoformat()
        if date_str != today_str:
            raise ValidationError("只能取消当天的打卡", code=CANNOT_CANCEL_PAST_CHECKIN)

        existing = get_checkin_by_date(habit_id, date_str)
        if not existing:
            raise NotFoundError("该日期无打卡记录", code=CHECKIN_NOT_FOUND)

        challenge = get_challenge_by_id(existing["challenge_id"])
        if not challenge or challenge["status"] != "in_progress":
            raise ValidationError("挑战已结束，无法取消打卡", code=CANNOT_CANCEL_PAST_CHECKIN)

        delete_checkin(habit_id, date_str)
        new_count = max(challenge["completed_count"] - 1, 0)
        update_challenge(challenge["id"], {"completed_count": new_count})

        settlement = self._judge_challenge_result(habit_id, challenge["id"], True, False)
        habit_item = self._build_habit_response(get_habit_by_id(habit_id))
        return CancelCheckInResponse(habit=habit_item, settlement=settlement)

    def _validate_backfill_target_date(
        self, target_date: date, today: date, start_date: date, end_date: date
    ) -> str | None:
        if target_date >= today:
            return "今日打卡请使用打卡接口"
        if (today - target_date).days > 6:
            return "只能补签过去 6 天内的日期"
        if target_date < start_date or target_date > end_date:
            return "补签日期不在当前挑战周期内"
        return None

    def backfill_checkin(
        self, habit_id: str, req: BackfillCheckInRequest
    ) -> BackfillCheckInBatchResponse:
        row = get_habit_by_id(habit_id)
        if not row:
            raise NotFoundError("习惯不存在", code=HABIT_NOT_FOUND)
        if row["status"] != "active":
            raise ValidationError("习惯处于暂停状态，无法补签", code=HABIT_NOT_ACTIVE)

        challenge = get_challenge_by_id(req.challenge_id)
        if not challenge or challenge["habit_id"] != habit_id:
            raise NotFoundError("挑战不存在", code=CHALLENGE_NOT_FOUND)

        today = get_local_today()
        seen_dates: set[str] = set()
        results: list[BackfillCheckInResultItem] = []

        def _append_failed(date_str: str, message: str, error_code: str):
            results.append(
                BackfillCheckInResultItem(
                    date=date_str,
                    status="failed",
                    checkin=None,
                    settlement=None,
                    error_code=error_code,
                    message=message,
                )
            )

        for item in req.items:
            date_str = item.date
            if date_str in seen_dates:
                _append_failed(date_str, "请求内存在重复补签日期", CHECKIN_ALREADY_EXISTS)
                continue
            seen_dates.add(date_str)

            try:
                target_date = date.fromisoformat(date_str)
            except ValueError:
                _append_failed(date_str, "补签日期格式无效", BACKFILL_DATE_OUT_OF_WINDOW)
                continue

            latest_challenge = get_challenge_by_id(req.challenge_id)
            if not latest_challenge or latest_challenge["habit_id"] != habit_id:
                raise NotFoundError("挑战不存在", code=CHALLENGE_NOT_FOUND)
            if latest_challenge["status"] != "in_progress":
                _append_failed(date_str, "挑战已结束，无法补签", BACKFILL_DATE_OUT_OF_WINDOW)
                continue

            start_date = date.fromisoformat(latest_challenge["start_date"])
            end_date = date.fromisoformat(latest_challenge["end_date"])
            date_error = self._validate_backfill_target_date(target_date, today, start_date, end_date)
            if date_error:
                _append_failed(date_str, date_error, BACKFILL_DATE_OUT_OF_WINDOW)
                continue

            now_str = get_utc_now_iso()
            checkin_id = create_checkin(
                {"habit_id": habit_id, "challenge_id": latest_challenge["id"], "date": date_str}
            )
            if not checkin_id:
                _append_failed(date_str, "该日期已有打卡记录", CHECKIN_ALREADY_EXISTS)
                continue

            new_count = latest_challenge["completed_count"] + 1
            update_challenge(latest_challenge["id"], {"completed_count": new_count})

            settlement = self._judge_challenge_result(habit_id, latest_challenge["id"], True, False)
            checkin_obj = CheckInObject(
                id=checkin_id,
                habit_id=habit_id,
                challenge_id=latest_challenge["id"],
                date=date_str,
                completed=True,
                completed_at=now_str,
                created_at=now_str,
            )
            results.append(
                BackfillCheckInResultItem(
                    date=date_str,
                    status="succeeded",
                    checkin=checkin_obj,
                    settlement=settlement,
                    error_code=None,
                    message=None,
                )
            )

        habit_item = self._build_habit_response(get_habit_by_id(habit_id))
        succeeded_count = sum(1 for item in results if item.status == "succeeded")
        failed_count = len(results) - succeeded_count
        summary = BackfillCheckInBatchSummary(
            total=len(results), succeeded=succeeded_count, failed=failed_count
        )
        return BackfillCheckInBatchResponse(habit=habit_item, results=results, summary=summary)


def update_habit_level(habit_id: str, new_level: int) -> bool:
    """更新习惯等级（挑战成功结算用）。"""
    affected = db.execute(
        "UPDATE habits SET current_level = ?, updated_at = ? WHERE id = ?",
        (new_level, get_utc_now_iso(), habit_id),
    )
    return affected > 0


_habit_service_instance: HabitService | None = None


def get_habit_service() -> HabitService:
    """获取 HabitService 单例（延迟初始化，避免循环导入）。"""
    global _habit_service_instance
    if _habit_service_instance is None:
        _habit_service_instance = HabitService()
    return _habit_service_instance