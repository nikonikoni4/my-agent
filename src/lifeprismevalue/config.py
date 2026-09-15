"""lifeprismevalue 配置

集中维护本模块的路径与时区配置，允许通过环境变量覆盖（测试用副本库时设置）。

- LIFEPRISMEVALUE_DB_PATH : 覆盖数据库路径（默认 agent/lifeprismData/dataset/lifewatch_ai.db）
- LIFEPRISMEVALUE_DATA_PATH: 覆盖 lifeprism 数据根目录（默认 agent/lifeprismData）
- LIFEPRISMEVALUE_TIMEZONE: 覆盖本地时区（默认 Asia/Shanghai）
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


def get_lifeprism_data_path() -> Path:
    """返回 lifeprism 数据根目录。"""
    return _lifeprism_data_path


def get_session_path() -> Path:
    """返回 session 目录路径。"""
    return SESSION_PATH


def get_user_timezone() -> str:
    """返回用户本地时区 IANA 名称。"""
    return _timezone