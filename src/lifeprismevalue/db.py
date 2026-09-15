"""统一的 sqlite 数据访问层

移植自 lifeprism.repository.database_manager，去掉 pandas 依赖，统一使用
标准库 sqlite3。支持读写与只读（测试用副本库）两种模式，提供 get_connection()
上下文管理器，自动提交 / 回滚 / 异常封装。

所有业务数据访问函数（data/ 下）与工具（tools/ 下）只依赖这里的 connect()
与 get_connection()，避免 On/Off 不同的数据源耦合。默认使用 config 中配置的
数据库路径，测试可通过 lifeprismevalue.config 覆盖。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import logging

from lifeprismevalue import config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class DataAccessError(Exception):
    """数据库访问异常。"""


def connect(readonly: bool = False) -> sqlite3.Connection:
    """创建一个标准连接。

    Args:
        readonly: 是否为只读模式（仅 SELECT，用于测试副本库等场景）。

    Returns:
        sqlite3.Connection: row_factory 为 sqlite3.Row 的连接。
    """
    db_path = Path(config.get_db_path())
    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_connection(readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """数据库连接上下文管理器（每次获取一条独立连接，用后即关）。

    Yields:
        sqlite3.Connection: 数据库连接。

    Raises:
        DataAccessError: 连接或操作失败时抛出。
    """
    conn = connect(readonly=readonly)
    try:
        yield conn
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        logger.error("数据库操作失败，已回滚: db_path=%s, error=%s", config.get_db_path(), e)
        raise DataAccessError(f"数据库操作失败: {e}") from e
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query_all(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    """执行查询，返回字典列表。"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        if not rows:
            return []
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row, strict=False)) for row in rows]


def query_one(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    """执行查询，返回单条字典或 None。"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row, strict=False))


def execute(sql: str, params: tuple | list = (), commit: bool = True) -> int:
    """执行写操作（INSERT/UPDATE/DELETE），返回受影响行数。"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        return cursor.rowcount


def execute_lastrowid(sql: str, params: tuple | list = ()) -> int | None:
    """执行 INSERT，返回自增 lastrowid。"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        return cursor.lastrowid