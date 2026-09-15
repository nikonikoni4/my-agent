"""心情数据访问层

移植自 lifeprism 的 mood_providers（mood_entries / mood_types / mood_impacts）。
"""

from __future__ import annotations

import uuid
from typing import Any

from lifeprismevalue import db
from lifeprismevalue.utils.time_utils import get_utc_now_iso

_MOOD_ENTRY_UPDATE_FIELDS = {
    "mood_type_id",
    "score",
    "content",
    "factors",
    "event_time",
}


def get_mood_types() -> list[dict[str, Any]]:
    """获取所有心情类型（按 sort_order DESC 排序）。"""
    return db.query_all("SELECT * FROM mood_types ORDER BY sort_order DESC")


def get_mood_type_by_id(mood_type_id: str) -> dict[str, Any] | None:
    """按 ID 获取心情类型。"""
    return db.query_one("SELECT * FROM mood_types WHERE id = ?", (mood_type_id,))


def get_mood_impacts() -> list[dict[str, Any]]:
    """获取所有影响因素（按 sort_order DESC 排序）。"""
    return db.query_all("SELECT * FROM mood_impacts ORDER BY sort_order DESC")


def get_mood_entries(
    start_time: str | None = None, end_time: str | None = None
) -> list[dict[str, Any]]:
    """获取心情记录列表（按 event_time ASC 排序）。

    Args:
        start_time: UTC ISO，可选；使用 event_time >= 过滤
        end_time: UTC ISO，可选，不包含此时刻；使用 event_time < 过滤
    """
    conditions = []
    params = []
    if start_time:
        conditions.append("event_time >= ?")
        params.append(start_time)
    if end_time:
        conditions.append("event_time < ?")
        params.append(end_time)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return db.query_all(f"SELECT * FROM mood_entries{where} ORDER BY event_time ASC", params)


def create_mood_entry(data: dict[str, Any]) -> str:
    """创建心情记录，返回新 ID。"""
    new_id = f"mood-{str(uuid.uuid4())[:8]}"
    invalid = set(data.keys()) - _MOOD_ENTRY_UPDATE_FIELDS
    if invalid:
        raise ValueError(f"Invalid insert fields: {invalid}")

    insert = {"id": new_id, **data}
    if not insert.get("event_time"):
        insert["event_time"] = get_utc_now_iso()
    if not insert.get("created_at"):
        insert["created_at"] = get_utc_now_iso()
    if not insert.get("updated_at"):
        insert["updated_at"] = get_utc_now_iso()

    cols = ", ".join(insert.keys())
    ph = ", ".join("?" * len(insert))
    db.execute(f"INSERT INTO mood_entries ({cols}) VALUES ({ph})", list(insert.values()))
    return new_id