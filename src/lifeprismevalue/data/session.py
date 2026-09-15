"""会话查询数据访问层

移植自 lifeprism 的 session_query 工具逻辑（无数据库，读写 session/*.jsonl 与
chat_history.json 文件）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from lifeprismevalue import config
from lifeprismevalue.utils.time_utils import utc_to_local_display

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _extract_text(content: Any) -> str:
    """从消息 content 中提取文本（content 可能是 str 或 list）。"""
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return content if isinstance(content, str) else ("" if content is None else str(content))


def query_session_list(date_filter: str | None = None) -> dict[str, Any]:
    """查询会话列表，返回 {session_id: {updated_at, last_user_message}}。

    Args:
        date_filter: 可选，日期筛选 YYYY-MM-DD，只返回该日期更新的会话
    """
    if date_filter is not None and not _DATE_PATTERN.match(date_filter):
        raise ValueError("日期格式错误，应为 YYYY-MM-DD")

    session_path = config.get_session_path()
    if not session_path.exists():
        return {}

    session_data: dict[str, Any] = {}
    for file in session_path.glob("*.jsonl"):
        session_id = file.stem
        try:
            with open(file, encoding="utf-8") as f:
                metadata = None
                last_user_msg = None
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("_type") == "metadata":
                        metadata = data
                    elif data.get("role") == "user":
                        last_user_msg = _extract_text(data.get("content", ""))

                if not metadata:
                    continue
                updated_at = metadata.get("updated_at", "")
                if date_filter and not updated_at.startswith(date_filter):
                    continue
                session_data[session_id] = {
                    "updated_at": updated_at,
                    "last_user_message": last_user_msg or "",
                }
        except Exception:
            # 单个文件损坏跳过，不影响整体
            continue

    return session_data


def load_chat_histories() -> list[dict[str, Any]]:
    """加载 chat_history.json 的总结记录列表（文件不存在/损坏返回空列表）。"""
    path = config.CHAT_HISTORY_PATH
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def query_session_list_with_summary(date_filter: str | None = None) -> dict[str, Any]:
    """查询会话列表（含每个会话最新总结），返回 {session_id: {last_summary, last_user_message}}。"""
    session_data = query_session_list(date_filter)

    summaries: dict[str, dict[str, str]] = {}
    for history in load_chat_histories():
        if "session_id" not in history:
            continue
        session_id = history.get("session_id")
        timestamp = history.get("timestamp", "")
        content = history.get("content", "")
        if session_id not in summaries or timestamp > summaries[session_id]["timestamp"]:
            summaries[session_id] = {"timestamp": timestamp, "content": content}

    result = {}
    for session_id, data in session_data.items():
        last_summary = summaries.get(session_id, {}).get("content", "")
        result[session_id] = {
            "last_summary": last_summary,
            "last_user_message": data["last_user_message"],
        }
    return result


def get_session_file_path(session_id: str):
    """返回会话文件路径（可能不存在）。"""
    return config.get_session_path() / f"{session_id}.jsonl"


def query_session_history(session_id: str, limit: int = 10) -> tuple[list[dict[str, Any]], str]:
    """查询指定会话的最近 N 轮对话（按时间倒序）。

    Returns:
        (messages, error)：messages 为 [{role, content, timestamp}]；
            error 非空表示失败原因（文件不存在）。
    """
    session_path = get_session_file_path(session_id)
    if not session_path.exists():
        return [], f"会话 {session_id} 不存在"

    messages = []
    try:
        with open(session_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("_type") == "metadata":
                    continue
                if data.get("role") in ["user", "assistant"]:
                    messages.append(
                        {
                            "role": data.get("role", ""),
                            "content": data.get("content", ""),
                            "timestamp": data.get("timestamp", ""),
                        }
                    )
    except Exception as e:
        return [], f"读取会话失败: {e}"

    messages.reverse()
    messages = messages[: min(limit, 50)]

    formatted = []
    for msg in messages:
        role = "用户" if msg["role"] == "user" else "助手"
        timestamp = msg["timestamp"]
        try:
            local_str = utc_to_local_display(timestamp)
            time_str = local_str[5:16]
        except Exception:
            time_str = timestamp[:16] if len(timestamp) >= 16 else timestamp

        content = msg["content"]
        if not content or (isinstance(content, str) and not content.strip()):
            content_str = "(空消息)"
        elif isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_parts.append(block)
            content_str = " ".join(text_parts) if text_parts else "(空消息)"
        else:
            content_str = str(content)

        if len(content_str) > 100:
            content_str = content_str[:80] + "...\n(内容较长，已省略)"
        content_str = re.sub(r"\n{3,}", "\n\n", content_str)
        formatted.append((role, time_str, content_str))

    return formatted, ""