"""lifeprismevalue 配置

集中维护本模块的路径与时区配置，允许通过环境变量覆盖（测试用副本库时设置）。

- LIFEPRISMEVALUE_DB_PATH : 覆盖数据库路径（默认 agent/lifeprismData/dataset/lifewatch_ai.db）
- LIFEPRISMEVALUE_DATA_PATH: 覆盖 lifeprism 数据根目录（默认 agent/lifeprismData）
- LIFEPRISMEVALUE_TIMEZONE: 覆盖本地时区（默认 Asia/Shanghai）

环境变量只决定进程启动时的默认值。运行时要把整个数据根切到别处（评测的工作副本、
测试的隔离副本），用 `use_data_path(root)`，它一次接管数据库路径与全部派生路径。
"""

from __future__ import annotations

import os
from pathlib import Path

# agent 项目根目录：src/lifeprismevalue/config.py 往上一级是 src，再往上一级是项目根
_AGENT_ROOT = Path(__file__).resolve().parents[2]

# lifeprism 数据目录（包含 dataset/db、user/daily_data、session 等）
DEFAULT_DATA_PATH = _AGENT_ROOT / "lifeprismData"
_lifeprism_data_path = Path(
    os.environ.get("LIFEPRISMEVALUE_DATA_PATH", str(DEFAULT_DATA_PATH))
).expanduser()

# 数据库路径
DEFAULT_DB_PATH = _lifeprism_data_path / "dataset" / "lifewatch_ai.db"
_db_path = Path(os.environ.get("LIFEPRISMEVALUE_DB_PATH", str(DEFAULT_DB_PATH))).expanduser()

# 本地时区（IANA 名称）
DEFAULT_TIMEZONE = "Asia/Shanghai"
_timezone = os.environ.get("LIFEPRISMEVALUE_TIMEZONE", DEFAULT_TIMEZONE)

# 派生路径
SESSION_PATH = _lifeprism_data_path / "session"
CHAT_HISTORY_PATH = _lifeprism_data_path / "user" / "daily_data" / "chat_history.json"
BOOTSTRAP_PATH = _lifeprism_data_path / "agent" / "chat" / "bootstrap.md"


def get_db_path() -> Path:
    """返回当前数据库路径。"""
    return _db_path


def use_data_path(root: str | Path) -> Path:
    """把整个数据根切到 root，并同步重算所有派生路径（评测隔离用）。

    为什么必须有这个入口：工具侧取数据库只经过本模块的全局路径
    （`db.connect()` → `get_db_path()`），拿不到调用方传的 `data_path`。评测每次
    run 都有一份工作副本 `runs/<run_id>/work/`，若不显式切根，工具会写进**真实**
    `lifeprismData` —— 写入"成功"却落在别处，而证据采集读的是副本，于是每轮都
    看到"无新增行"。切根必须发生在创建 agent、调用任何工具之前。

    显式调用优先于环境变量：本函数一旦被调用，`LIFEPRISMEVALUE_DATA_PATH` /
    `LIFEPRISMEVALUE_DB_PATH` 不再参与（它们只决定进程启动时的默认值）。

    Args:
        root: 新的数据根目录（含 `dataset/`、`user/`、`session/`、`prompts/`、`agent/`）。

    Returns:
        切换后的数据根目录。
    """
    global _lifeprism_data_path, DEFAULT_DB_PATH, _db_path
    global SESSION_PATH, CHAT_HISTORY_PATH, BOOTSTRAP_PATH

    _lifeprism_data_path = Path(root).expanduser()
    DEFAULT_DB_PATH = _lifeprism_data_path / "dataset" / "lifewatch_ai.db"
    _db_path = DEFAULT_DB_PATH
    SESSION_PATH = _lifeprism_data_path / "session"
    CHAT_HISTORY_PATH = _lifeprism_data_path / "user" / "daily_data" / "chat_history.json"
    BOOTSTRAP_PATH = _lifeprism_data_path / "agent" / "chat" / "bootstrap.md"
    return _lifeprism_data_path


def get_lifeprism_data_path() -> Path:
    """返回 lifeprism 数据根目录。"""
    return _lifeprism_data_path


def get_session_path() -> Path:
    """返回 session 目录路径。"""
    return SESSION_PATH


def get_user_timezone() -> str:
    """返回用户本地时区 IANA 名称。"""
    return _timezone