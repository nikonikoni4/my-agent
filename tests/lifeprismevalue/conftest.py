"""tests/lifeprismevalue 的共享固件。

所有测试基于 lifeprismData/dataset/lifewatch_ai - 副本.db 的隔离副本运行：
- 每 pytest 会话复制一份快照到临时目录，避免写测试污染原始副本库
- 运行前把 lifeprismevalue.config 的数据库指向该临时副本
（config.get_db_path() 每次调用都读取模块全局 _db_path，升层为测试可覆盖）
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from lifeprismevalue import config

# 快照库路径（tests/lifeprismevalue/conftest.py -> agent 根 -> lifeprismData/dataset）
_SNAPSHOT_DB = Path(__file__).resolve().parents[2] / "lifeprismData" / "dataset" / "lifewatch_ai - 副本.db"


def _set_db_path(path: Path) -> None:
    """运行时覆盖 config 的数据库路径（升层可测试）。"""
    config._db_path = path  # config.get_db_path()/connect 均读取该全局
    config.DEFAULT_DB_PATH = path


@pytest.fixture(scope="session", autouse=True)
def lw_snapshot_db(tmp_path_factory) -> Path:
    """会话级隔离副本：拷其快照库到临时目录并指向 config（autouse 保证所有
    存取都落在临时副本而非真实库）。

    Returns:
        Path: 临时副本库路径。
    """
    assert _SNAPSHOT_DB.exists(), f"快照库不存在: {_SNAPSHOT_DB}"
    tmp = tmp_path_factory.mktemp("lw_iso_db") / _SNAPSHOT_DB.name
    shutil.copy2(_SNAPSHOT_DB, tmp)
    _set_db_path(Path(tmp))
    return Path(tmp)