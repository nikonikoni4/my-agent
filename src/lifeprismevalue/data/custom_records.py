"""自定义记录数据访问层

移植自 lifeprism 的 custom_record_aggregator。Meta 表驱动：custom_record_types +
custom_record_fields 定义动态数据表结构，数据表（custom_<slug>）动态创建。

仅移植工具用到的能力：list_types / create_type / create_entry / query_entries，
以及其依赖的校验、字段过滤、动态表 DDL 逻辑。
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from lifeprismevalue import db
from lifeprismevalue.data.exceptions import (
    DataAccessError,
    DuplicateEntityError,
    EntityNotFoundError,
    ValidationError,
)

# 类型转换失败哨兵（不直接用 None，避免与合法 NULL 混淆）
_INVALID_SENTINEL = object()

_TYPE_ID_PREFIX = "crt-"
_FIELD_ID_PREFIX = "crf-"
_ENTRY_ID_PREFIX = "cre-"
_DATA_TABLE_PREFIX = "custom_"

_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_FIELD_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

_FILTER_OP_TO_SQL = {
    "eq": "=",
    "ne": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}

_TEXT_FILTER_OPS = {"eq", "ne", "in", "contains"}
_NUMERIC_FILTER_OPS = {"eq", "ne", "in", "gt", "gte", "lt", "lte"}

_FIELD_TYPE_TO_SQL = {
    "text": "TEXT",
    "integer": "INTEGER",
    "float": "REAL",
}


def generate_create_table_ddl(slug: str, fields: list[dict[str, Any]]) -> str:
    """生成动态数据表的 CREATE TABLE DDL 语句。"""
    data_table = f"{_DATA_TABLE_PREFIX}{slug}"
    column_defs = ["id TEXT PRIMARY KEY"]
    for f in fields:
        ftype = f.get("field_type", "text")
        sql_type = _FIELD_TYPE_TO_SQL.get(ftype, "TEXT")
        column_defs.append(f"{f['field_key']} {sql_type}")
    column_defs.append("event_time TEXT")
    column_defs.append("created_at TEXT")
    column_defs.append("updated_at TEXT")
    return f"CREATE TABLE {data_table} ({', '.join(column_defs)})"


def _query_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    """查询单条记录，返回字典或 None。"""
    return db.query_one(sql, params)


def _query_all(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """查询多条记录，返回字典列表。"""
    return db.query_all(sql, params)


def _get_fields_by_type_id(type_id: str) -> list[dict[str, Any]]:
    """按 type_id 获取字段定义列表（按 sort_order 排序）。"""
    return _query_all(
        "SELECT id, field_name, field_key, field_type, sort_order, display_role "
        "FROM custom_record_fields WHERE type_id = ? ORDER BY sort_order ASC",
        (type_id,),
    )


def _get_type_and_table(type_id: str) -> tuple[dict[str, Any], str]:
    """按 type_id 获取类型元信息 + 数据表名。类型不存在抛 EntityNotFoundError。"""
    t = _query_one("SELECT id, name, slug FROM custom_record_types WHERE id = ?", (type_id,))
    if t is None:
        raise EntityNotFoundError(entity_type="CustomRecordType", entity_id=type_id)
    data_table = f"{_DATA_TABLE_PREFIX}{t['slug']}"
    return t, data_table


def _coerce_field_value(field_key: str, value: Any, field_type: str) -> Any:
    """按 field_type 校验并转换 data 字段值；失败返回 _INVALID_SENTINEL。"""
    if value is None:
        return None

    if field_type == "text":
        return str(value)

    if field_type == "integer":
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return _INVALID_SENTINEL
        if isinstance(value, str):
            stripped = value.strip()
            if "." in stripped:
                return _INVALID_SENTINEL
            try:
                return int(stripped)
            except ValueError:
                return _INVALID_SENTINEL
        return _INVALID_SENTINEL

    if field_type == "float":
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return _INVALID_SENTINEL
        return _INVALID_SENTINEL

    return _INVALID_SENTINEL


def list_types() -> list[dict[str, Any]]:
    """列出自定义记录类型（含 fields）。"""
    try:
        types = _query_all(
            "SELECT id, name, slug, description, card_template, icon, accent_color, "
            "created_at, updated_at FROM custom_record_types ORDER BY created_at ASC"
        )
        for t in types:
            t["fields"] = _get_fields_by_type_id(t["id"])
        return types
    except sqlite3.Error as e:
        raise DataAccessError("列出自定义记录类型失败", {"error": str(e)}, e) from e


def create_type(
    name: str,
    slug: str,
    fields: list[dict[str, Any]],
    description: str | None = None,
) -> str:
    """创建自定义记录类型，返回 type_id。"""
    if not _SLUG_PATTERN.match(slug):
        raise ValidationError(
            f"slug 格式无效: {slug}（要求 ^[a-z][a-z0-9_]*$）",
            code="INVALID_SLUG_FORMAT",
            details={"slug": slug},
        )
    if not fields:
        raise ValidationError("fields 不能为空，至少需要 1 个字段", code="EMPTY_FIELDS")
    for f in fields:
        if not _FIELD_KEY_PATTERN.match(f["field_key"]):
            raise ValidationError(
                f"field_key 格式无效: {f['field_key']}（要求 ^[a-z][a-z0-9_]*$）",
                code="INVALID_FIELD_KEY_FORMAT",
                details={"field_key": f["field_key"]},
            )
    for f in fields:
        ftype = f.get("field_type", "text")
        if ftype not in _FIELD_TYPE_TO_SQL:
            raise ValidationError(
                f"field_type 无效: {ftype}（可选 text/integer/float）",
                code="INVALID_FIELD_TYPE",
                details={"field_type": ftype, "valid_types": list(_FIELD_TYPE_TO_SQL.keys())},
            )
    seen_keys = set()
    for f in fields:
        if f["field_key"] in seen_keys:
            raise ValidationError(
                f"field_key 重复: {f['field_key']}",
                code="DUPLICATE_FIELD_KEY",
                details={"field_key": f["field_key"]},
            )
        seen_keys.add(f["field_key"])

    existing = _query_one("SELECT id FROM custom_record_types WHERE slug = ?", (slug,))
    if existing:
        raise DuplicateEntityError("CustomRecordType", slug, conflict_field="slug")

    type_id = f"{_TYPE_ID_PREFIX}{uuid.uuid4().hex[:8]}"
    data_table = f"{_DATA_TABLE_PREFIX}{slug}"
    now = datetime.now(timezone.utc).isoformat()

    try:
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO custom_record_types "
                "(id, name, slug, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (type_id, name, slug, description or "", now, now),
            )
            for idx, f in enumerate(fields):
                field_id = f"{_FIELD_ID_PREFIX}{uuid.uuid4().hex[:8]}"
                cursor.execute(
                    "INSERT INTO custom_record_fields "
                    "(id, type_id, field_name, field_key, field_type, sort_order, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        field_id,
                        type_id,
                        f["field_name"],
                        f["field_key"],
                        f.get("field_type", "text"),
                        f.get("sort_order", idx),
                        now,
                        now,
                    ),
                )
            ddl = generate_create_table_ddl(slug, fields)
            cursor.execute(ddl)
            return type_id
    except sqlite3.IntegrityError as e:
        raise DuplicateEntityError("CustomRecordType", slug, conflict_field="slug") from e
    except sqlite3.Error as e:
        raise DataAccessError(
            "创建自定义记录类型失败", {"name": name, "slug": slug, "error": str(e)}, e
        ) from e


def create_entry(type_id: str, data: dict[str, Any], event_time: str | None = None) -> str:
    """录入一条记录到 custom_<slug> 表，返回 entry_id。"""
    _, data_table = _get_type_and_table(type_id)
    fields = _get_fields_by_type_id(type_id)
    valid_keys = {f["field_key"] for f in fields}

    invalid_keys = set(data.keys()) - valid_keys
    if invalid_keys:
        valid_fields = [
            {"field_key": f["field_key"], "field_name": f["field_name"]} for f in fields
        ]
        raise ValidationError(
            f"字段不存在: {','.join(sorted(invalid_keys))}",
            code="INVALID_FIELD_KEY",
            details={"invalid_keys": sorted(invalid_keys), "valid_fields": valid_fields},
        )

    field_type_map = {f["field_key"]: f["field_type"] for f in fields}
    invalid_value_fields: list[dict] = []
    for key, value in data.items():
        ftype = field_type_map[key]
        converted = _coerce_field_value(key, value, ftype)
        if converted is _INVALID_SENTINEL:
            invalid_value_fields.append({"field_key": key, "value": value, "expected_type": ftype})
        else:
            data[key] = converted

    if invalid_value_fields:
        valid_fields = [
            {"field_key": f["field_key"], "field_name": f["field_name"], "field_type": f["field_type"]}
            for f in fields
        ]
        raise ValidationError(
            f"字段值类型不匹配: {','.join(iv['field_key'] for iv in invalid_value_fields)}",
            code="INVALID_FIELD_VALUE",
            details={"invalid_fields": invalid_value_fields, "valid_fields": valid_fields},
        )

    entry_id = f"{_ENTRY_ID_PREFIX}{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    event_time_val = event_time if event_time else now

    columns = ["id", "event_time", "created_at", "updated_at"]
    placeholders = ["?", "?", "?", "?"]
    values: list[Any] = [entry_id, event_time_val, now, now]
    for key in data:
        columns.append(key)
        placeholders.append("?")
        values.append(data[key])

    sql = f"INSERT INTO {data_table} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
    try:
        db.execute(sql, tuple(values))
        return entry_id
    except sqlite3.Error as e:
        raise DataAccessError(
            "录入自定义记录失败", {"type_id": type_id, "error": str(e)}, e
        ) from e


def _build_field_filters(
    type_id: str, filters: list[dict[str, Any]]
) -> tuple[list[str], list[Any]]:
    """构建字段级过滤 WHERE 子句（全参数化，field_key 已对照字段定义校验）。"""
    fields = _get_fields_by_type_id(type_id)
    field_type_map = {f["field_key"]: f["field_type"] for f in fields}

    clauses: list[str] = []
    params: list[Any] = []
    for flt in filters:
        key = flt.get("field_key")
        op = flt.get("op")
        value = flt.get("value")

        if key not in field_type_map:
            valid_fields = [
                {"field_key": f["field_key"], "field_name": f["field_name"], "field_type": f["field_type"]}
                for f in fields
            ]
            raise ValidationError(
                f"过滤字段不存在: {key}",
                code="INVALID_FIELD_KEY",
                details={"invalid_keys": [key], "valid_fields": valid_fields},
            )
        ftype = field_type_map[key]

        allowed_ops = _TEXT_FILTER_OPS if ftype == "text" else _NUMERIC_FILTER_OPS
        if op not in allowed_ops:
            raise ValidationError(
                f"过滤操作符无效: {op}（字段 {key} 类型 {ftype}）",
                code="INVALID_FILTER_OP",
                details={"field_key": key, "op": op, "allowed_ops": sorted(allowed_ops)},
            )

        if op == "in":
            if not isinstance(value, list) or not value:
                raise ValidationError(
                    f"过滤值无效: op=in 要求 value 为非空数组（字段 {key}）",
                    code="INVALID_FIELD_VALUE",
                    details={
                        "invalid_fields": [
                            {"field_key": key, "value": value, "expected_type": f"{ftype} 数组"}
                        ]
                    },
                )
            converted: list[Any] = []
            for v in value:
                cv = _coerce_field_value(key, v, ftype)
                if cv is _INVALID_SENTINEL:
                    raise ValidationError(
                        f"过滤值类型不匹配: {key}",
                        code="INVALID_FIELD_VALUE",
                        details={"invalid_fields": [{"field_key": key, "value": v, "expected_type": ftype}]},
                    )
                converted.append(cv)
            placeholders = ",".join("?" * len(converted))
            clauses.append(f"{key} IN ({placeholders})")
            params.extend(converted)
            continue

        if op == "contains":
            if value is None:
                raise ValidationError(
                    f"过滤值无效: op=contains 要求 value 为非空值（字段 {key}）",
                    code="INVALID_FIELD_VALUE",
                    details={
                        "invalid_fields": [
                            {"field_key": key, "value": value, "expected_type": "text"}
                        ]
                    },
                )
            text_val = value if isinstance(value, str) else str(value)
            escaped = text_val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(f"{key} LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
            continue

        cv = _coerce_field_value(key, value, ftype)
        if cv is _INVALID_SENTINEL or cv is None:
            raise ValidationError(
                f"过滤值类型不匹配: {key}",
                code="INVALID_FIELD_VALUE",
                details={"invalid_fields": [{"field_key": key, "value": value, "expected_type": ftype}]},
            )
        sql_op = _FILTER_OP_TO_SQL[op]
        clauses.append(f"{key} {sql_op} ?")
        params.append(cv)

    return clauses, params


def query_entries(
    type_id: str,
    date_range: tuple[str | None, str | None] | None = None,
    page: int = 1,
    page_size: int = 50,
    filters: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """按时间范围与字段级过滤条件分页查询记录（按 event_time DESC 排序）。"""
    _, data_table = _get_type_and_table(type_id)

    where_clauses: list[str] = []
    params: list[Any] = []
    if date_range:
        start, end = date_range
        if start:
            where_clauses.append("event_time >= ?")
            params.append(start)
        if end:
            where_clauses.append("event_time <= ?")
            params.append(end)
    if filters:
        filter_clauses, filter_params = _build_field_filters(type_id, filters)
        where_clauses.extend(filter_clauses)
        params.extend(filter_params)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    offset = (page - 1) * page_size

    count_sql = f"SELECT COUNT(*) FROM {data_table} {where_sql}"
    data_sql = (
        f"SELECT * FROM {data_table} {where_sql} ORDER BY event_time DESC LIMIT ? OFFSET ?"
    )
    data_params = list(params) + [page_size, offset]

    try:
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(count_sql, tuple(params))
            total_count = cursor.fetchone()[0]
            cursor.execute(data_sql, tuple(data_params))
            rows = [dict(row) for row in cursor.fetchall()]
        return rows, total_count
    except sqlite3.Error as e:
        raise DataAccessError(
            "查询自定义记录失败", {"type_id": type_id, "error": str(e)}, e
        ) from e