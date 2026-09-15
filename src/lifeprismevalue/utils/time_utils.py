"""时间处理工具函数 - UTC 时区迁移统一入口

移植自 lifeprism/utils/time_utils.py，用标准库 zoneinfo 替代 pytz。

核心原则：
- 时间戳字段（start_time / end_time / event_time 等）使用 UTC
- 日期字段（date / start_date / end_date 等 YYYY-MM-DD 格式）保持本地时区日期
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from lifeprismevalue import config


def _get_tz() -> ZoneInfo:
    """获取用户本地时区。"""
    return ZoneInfo(config.get_user_timezone())


def get_local_today() -> date:
    """获取用户本地时区的今天日期。"""
    return datetime.now(_get_tz()).date()


def get_utc_now_iso() -> str:
    """获取当前 UTC 时间的 ISO 8601 格式字符串。"""
    return datetime.now(timezone.utc).isoformat()


def parse_iso_to_aware(iso_string: str) -> datetime:
    """将 ISO 8601 字符串解析为 aware datetime。

    对不带时区的输入按 UTC 处理，避免 naive / aware 比较错误。
    """
    dt = datetime.fromisoformat(iso_string)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local_to_utc_iso(local_str: str, format: str = "%Y-%m-%d %H:%M:%S") -> str:
    """将本地时间字符串转换为 UTC ISO 8601 格式字符串。"""
    tz = _get_tz()
    dt = datetime.strptime(local_str, format).replace(tzinfo=tz)
    return dt.astimezone(timezone.utc).isoformat()


def build_local_datetime(date_str: str, time_str: str = "00:00:00") -> str:
    """根据日期和时间构造本地时间字符串（用于校验后拼接）。"""
    combined = f"{date_str} {time_str}"
    datetime.strptime(combined, "%Y-%m-%d %H:%M:%S")
    return combined


def utc_to_local(utc_iso: str) -> datetime:
    """将 UTC ISO 8601 时间字符串转换为本地时区 datetime 对象。"""
    dt = parse_iso_to_aware(utc_iso)
    return dt.astimezone(_get_tz())


def utc_to_local_display(utc_iso: str) -> str:
    """将 UTC ISO 8601 时间字符串转换为本地时区显示格式 YYYY-MM-DD HH:MM:SS。"""
    return utc_to_local(utc_iso).strftime("%Y-%m-%d %H:%M:%S")


def build_utc_time_range(local_date: str) -> tuple[str, str]:
    """根据本地日期构造当天的 UTC 时间范围 (start_iso, end_iso)。"""
    start_utc = local_to_utc_iso(f"{local_date} 00:00:00")
    end_utc = local_to_utc_iso(f"{local_date} 23:59:59")
    return (start_utc, end_utc)