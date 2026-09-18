"""只读读 sqlite 的小工具：底座、环境、基线三边都是 sqlite 文件。

**为什么单独成模块**：环境机制（`env.py` 建库、开跑前自检）与取证（`evidence.py`
全库扫描）都要「只读打开 + 列表名」。两边各写一份就会漂移，而这两处漂移的后果都是
**不报错的那种错**：读的库不是同一个、或把 `sqlite_` 内部表当业务表比出差异。

只放"怎么读"（连接与列表名），不放"怎么比"——比对语义分别属于各自的模块。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    """只读地打开一个 sqlite 文件。

    不用普通 `connect`：读一个库也可能写它——WAL 库会顺带建 `-wal` / `-shm`，
    带热日志的库会被那次连接做一次回滚恢复。底座是只读的，任何写入都不该发生。
    `as_uri()` 会做百分号编码，非 ASCII 路径（本项目路径里就有中文）也安全。
    """
    return sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)


def table_names(db_path: str | Path) -> set[str]:
    """列出库里的业务表名（跳过 `sqlite_%` 内部表，如 `sqlite_sequence`）。"""
    con = open_readonly(db_path)
    try:
        return {
            name
            for (name,) in con.execute(
                "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
            )
        }
    finally:
        con.close()


def quote_identifier(name: str) -> str:
    """把表名 / 列名包成 SQLite 标识符（名字可能含空格或与关键字重名）。"""
    return '"' + name.replace('"', '""') + '"'
