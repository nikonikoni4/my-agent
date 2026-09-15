"""数据密度计算和时间段识别工具函数

移植自 lifeprism/llm/utils/density_utils.py，pure Python。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _to_dt(value: str) -> datetime:
    """将 ISO 格式字符串转换为 aware datetime 对象（naive 按 UTC 处理）。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def compute_bucket_density(bucket_start: str, bucket_end: str, logs: list[dict]) -> float:
    """计算时间桶内的活动密度 = 桶内有覆盖的秒数 / 桶总秒数。"""
    start_dt = _to_dt(bucket_start)
    end_dt = _to_dt(bucket_end)
    bucket_seconds = int((end_dt - start_dt).total_seconds())
    if bucket_seconds <= 0:
        return 0.0

    overlap_seconds = 0
    for row in logs:
        row_start = _to_dt(row["start_time"])
        row_end = _to_dt(row["end_time"])
        start_overlap = max(start_dt, row_start)
        end_overlap = min(end_dt, row_end)
        if end_overlap > start_overlap:
            overlap_seconds += int((end_overlap - start_overlap).total_seconds())

    return overlap_seconds / bucket_seconds


def _collect_buckets(
    logs: list[dict],
    range_start: str,
    range_end: str,
    threshold: float,
    bucket_minutes: int,
) -> list[dict]:
    """将时间范围切分为固定大小的时间桶，并计算每个桶的密度和是否匹配阈值。"""
    start_dt = _to_dt(range_start)
    end_dt = _to_dt(range_end)
    bucket_span = timedelta(minutes=bucket_minutes)
    cursor = start_dt
    buckets = []

    while cursor < end_dt:
        bucket_end = min(cursor + bucket_span, end_dt)
        density = compute_bucket_density(cursor.isoformat(), bucket_end.isoformat(), logs)
        buckets.append(
            {
                "start": cursor,
                "end": bucket_end,
                "density": density,
                "matched": density >= threshold,
            }
        )
        cursor = bucket_end

    return buckets


def _build_segment_item(merged_buckets: list[dict], segment_type: str) -> dict:
    """根据合并后的时间桶列表构建单个时间段的基本信息。"""
    segment_start = merged_buckets[0]["start"]
    segment_end = merged_buckets[-1]["end"]
    duration_seconds = int((segment_end - segment_start).total_seconds())

    return {
        "start": segment_start.isoformat(),
        "end": segment_end.isoformat(),
        "duration_seconds": duration_seconds,
        "segment_type": segment_type,
    }


def build_time_segments(
    logs: list[dict],
    range_start: str,
    range_end: str,
    threshold: float,
    min_duration_minutes: int,
    segment_type: str = "active",
    bucket_minutes: int = 10,
    max_bridge_buckets: int = 1,
) -> list[dict]:
    """识别并构建高密度时间段列表。

    Args:
        logs: 活动日志，每项含 start_time, end_time, duration
        range_start / range_end: 分析范围（ISO 格式）
        threshold: 密度阈值（0~1）
        min_duration_minutes: 最小段时长（分钟）
        segment_type: 段类型标识，默认 "active"
        bucket_minutes: 时间桶大小（分钟），默认 10
        max_bridge_buckets: 最大桥接桶数量，默认 1

    Returns:
        list[dict]: 每项含 start, end, duration_seconds, segment_type
    """
    buckets = _collect_buckets(logs, range_start, range_end, threshold, bucket_minutes)
    segments: list[dict] = []
    current: list[dict] = []
    bridge_count = 0

    def flush_current() -> None:
        if not current:
            return
        duration_seconds = int((current[-1]["end"] - current[0]["start"]).total_seconds())
        if duration_seconds >= min_duration_minutes * 60:
            segments.append(_build_segment_item(current, segment_type))

    for bucket in buckets:
        if bucket["matched"]:
            current.append(bucket)
            bridge_count = 0
            continue

        if current and bridge_count < max_bridge_buckets:
            current.append(bucket)
            bridge_count += 1
            continue

        flush_current()
        current = []
        bridge_count = 0

    flush_current()
    return segments