"""用户行为数据访问层

移植自 lifeprism 的 computer_usage / custom_block / behavior_analysis / todo
相关 Provider，去掉 QueryOptions 通用查询，改为针对工具的直白参数化 SQL。

涉及表：user_app_behavior_log / category / sub_category / timeline_custom_block /
        behavior_analysis / todo_list
"""

from __future__ import annotations

from typing import Any

from lifeprismevalue import db
from lifeprismevalue.utils.time_utils import get_utc_now_iso


def _select_fields(requested: list[str] | None, allowed: set[str]) -> str:
    """从请求的字段列表里挑出白名单内字段，拼出逗号分隔的列名。"""
    if not requested:
        return "*"
    cols = [c for c in requested if c in allowed]
    if not cols:
        return "*"
    return ", ".join(cols)


# ---------- 电脑使用数据（user_app_behavior_log） ----------

_COMPUTER_FIELDS = {
    "id",
    "start_time",
    "end_time",
    "duration",
    "app",
    "title",
    "is_multipurpose_app",
    "category_id",
    "sub_category_id",
    "link_to_goal_id",
}

_CATEGORY_SQL = (
    "SELECT {fields} FROM user_app_behavior_log "
    "WHERE start_time >= ? AND start_time <= ?"
)


def query_computer_usage_with_names(
    start_time: str, end_time: str, fields: list[str] | None = None
) -> list[dict[str, Any]]:
    """查询电脑使用日志，并附加 category_name / sub_category_name。

    Args:
        start_time / end_time: UTC ISO 时间范围（基于 start_time 字段，闭区间）
        fields: 需要的字段列表；None 表示全部

    Returns:
        list[dict]: 每条记录含请求字段 + category_name / sub_category_name
    """
    rows = db.query_all(
        _CATEGORY_SQL.format(fields=_select_fields(fields, _COMPUTER_FIELDS)),
        (start_time, end_time),
    )
    return _enrich_with_names(rows)


def _enrich_with_names(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为记录附加分类名称（依赖 category / sub_category 表）。"""
    if not records:
        return records

    category_ids = {r["category_id"] for r in records if r.get("category_id")}
    sub_ids = {r["sub_category_id"] for r in records if r.get("sub_category_id")}

    category_map: dict[str, str] = {}
    if category_ids:
        ph = ",".join("?" * len(category_ids))
        for row in db.query_all(f"SELECT id, name FROM category WHERE id IN ({ph})", tuple(category_ids)):
            category_map[row["id"]] = row["name"]

    sub_map: dict[str, str] = {}
    if sub_ids:
        ph = ",".join("?" * len(sub_ids))
        for row in db.query_all(
            f"SELECT id, name FROM sub_category WHERE id IN ({ph})", tuple(sub_ids)
        ):
            sub_map[row["id"]] = row["name"]

    for r in records:
        if r.get("category_id"):
            r["category_name"] = category_map.get(r["category_id"], "")
        if r.get("sub_category_id"):
            r["sub_category_name"] = sub_map.get(r["sub_category_id"], "")
    return records


# ---------- 用户自定义行为备注（timeline_custom_block） ----------

_CUSTOM_BLOCK_FIELDS = {
    "id",
    "start_time",
    "end_time",
    "duration",
    "content",
    "todo_id",
    "color",
    "category_id",
    "sub_category_id",
    "created_at",
    "updated_at",
}

_CUSTOM_BLOCK_UPDATE_FIELDS = {
    "start_time",
    "end_time",
    "duration",
    "content",
    "todo_id",
    "color",
    "category_id",
    "sub_category_id",
}


def query_custom_blocks(
    start_time: str, end_time: str, fields: list[str] | None = None
) -> list[dict[str, Any]]:
    """查询时间范围内的用户自定义行为备注。"""
    return db.query_all(
        "SELECT {fields} FROM timeline_custom_block "
        "WHERE start_time >= ? AND start_time <= ?".format(
            fields=_select_fields(fields, _CUSTOM_BLOCK_FIELDS)
        ),
        (start_time, end_time),
    )


def get_custom_block_by_id(block_id: int) -> dict[str, Any] | None:
    """按 ID 获取自定义行为备注。"""
    return db.query_one(
        "SELECT * FROM timeline_custom_block WHERE id = ? ORDER BY id", (block_id,)
    )


def create_custom_block(data: dict[str, Any]) -> dict[str, Any]:
    """创建用户自定义行为备注，返回创建后的完整记录。"""
    invalid = set(data.keys()) - _CUSTOM_BLOCK_UPDATE_FIELDS
    if invalid:
        raise ValueError(f"Invalid insert fields: {invalid}")

    now = get_utc_now_iso()
    fields = {**data, "created_at": now, "updated_at": now}
    cols = ", ".join(fields.keys())
    ph = ", ".join("?" * len(fields))
    block_id = db.execute_lastrowid(
        f"INSERT INTO timeline_custom_block ({cols}) VALUES ({ph})", list(fields.values())
    )
    return get_custom_block_by_id(int(block_id)) if block_id else {}


def update_custom_block(block_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    """更新用户自定义行为备注，返回更新后的完整记录或 None。"""
    nullable_fields = {"todo_id", "category_id", "sub_category_id"}
    update_data = {}
    for k, v in data.items():
        if k in nullable_fields:
            update_data[k] = v
        elif v is not None:
            update_data[k] = v

    if not update_data:
        return get_custom_block_by_id(block_id)

    invalid = set(update_data.keys()) - _CUSTOM_BLOCK_UPDATE_FIELDS
    if invalid:
        raise ValueError(f"Invalid update fields: {invalid}")

    update_data["updated_at"] = get_utc_now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in update_data)
    affected = db.execute(
        f"UPDATE timeline_custom_block SET {set_clause} WHERE id = ?",
        list(update_data.values()) + [block_id],
    )
    if not affected:
        return None
    return get_custom_block_by_id(block_id)


# ---------- AI 行为分析（behavior_analysis） ----------

_BEHAVIOR_FIELDS = {
    "start_time",
    "end_time",
    "behavior",
    "behavior_summary",
    "title",
    "screen_count",
    "created_at",
}


def query_behaviors(
    start_time: str, end_time: str, fields: list[str] | None = None
) -> list[dict[str, Any]]:
    """查询时间范围内的 AI 行为分析记录。"""
    return db.query_all(
        "SELECT {fields} FROM behavior_analysis "
        "WHERE start_time >= ? AND start_time <= ?".format(
            fields=_select_fields(fields, _BEHAVIOR_FIELDS)
        ),
        (start_time, end_time),
    )


# ---------- 待办事项（todo_list） ----------

_TODO_FIELDS = {
    "id",
    "order_index",
    "pool_order_index",
    "content",
    "color",
    "state",
    "link_to_goal_id",
    "date",
    "expected_finished_at",
    "actual_finished_at",
    "cross_day",
    "folder_id",
    "parent_id",
    "plan_doc_id",
    "delay_days",
    "delay_reason",
    "waid_order",
    "created_at",
    "updated_at",
}


def query_todos(
    start_date: str, end_date: str, fields: list[str] | None = None
) -> list[dict[str, Any]]:
    """按本地日期范围查询待办事项（date 字段为本地日期）。"""
    return db.query_all(
        "SELECT {fields} FROM todo_list "
        "WHERE date >= ? AND date <= ?".format(
            fields=_select_fields(fields, _TODO_FIELDS)
        ),
        (start_date, end_date),
    )