"""session 的加载恢复组件，与 SessionPresist（写）相对。

职责：按 session_id 定位 SessionPresist 写出的 jsonl 文件，还原为
SessionMetaData + list[SessionRecordData]，供上层构造 Session。

还原规则：
1. 第 1 行 meta 还原为 SessionMetaData（cwd 未落盘，暂恢复为空串）
2. 普通记录：按 Session.RECORD_DATA_TYPES 把 data dict 还原为类型实例，
   嵌套的 Message/ChatParams/Usage 逐层还原
3. text-chunk 打包行：解包回 merge 前的多条 assistant/chunk——
   seq 从 source_event_seqs 逐位恢复（首条即 source_event_seqs[0]），
   timestamp 按组首 + dt 累积重建，id/name 只还原到组内首条，
   与打包时"仅首条携带"的规则对齐

文件格式（与 SessionPresist 写入约定一致）：
  第 1 行：SessionMetaData.meta_data() 的 dict
  第 2 行起：SessionRecordData.to_record_dict() 的 dict
"""

import json
import logging
import datetime

from myagent.config.system_config import local_data_path
from myagent.agent.core.session.session import Session
from myagent.agent.core.session.types import SessionMetaData, SessionRecordData, AssistantChunkData
from myagent.agent.core.provider import Message, ToolCallRequest, ChatParams, Usage, StreamChunk

logger = logging.getLogger(__name__)








def _restore_message(d: dict) -> Message:
    """把落盘的 message dict 还原为 Message，tool_calls 同步还原"""
    tool_calls = [ToolCallRequest(**tc) for tc in (d.get("tool_calls") or [])]
    return Message(
        role=d["role"],
        content=d["content"],
        tool_calls=tool_calls,
        tool_call_id=d.get("tool_call_id"),
        reasoning_content=d.get("reasoning_content"),
    )


def _restore_data(record_type: str, d: dict):
    """按登记表把 data dict 还原为对应的 Data 类型实例。

    嵌套对象（Message/ChatParams/Usage）逐层还原，其余字段原样透传。

    Returns:
        还原后的 SessionData 实例；类型不在登记表或无法还原时返回 None。
    """
    data_cls = Session.RECORD_DATA_TYPES.get(record_type)
    if data_cls is None:
        return None
    # assistant/chunk 是打包行的解包产物，文件里不应直接出现，
    # 且它只能从 StreamChunk 构造、无法从 dict 还原
    if record_type == "assistant/chunk":
        logger.warning("文件中出现 assistant/chunk 行（应由 text-chunk 解包产生），跳过")
        return None
    if record_type in ("user/message", "assistant/message", "tool/result"):
        d = {**d, "message": _restore_message(d["message"])}
    elif record_type == "request/header":
        d = {**d, "params": ChatParams(**d["params"]) if d["params"] else None}
    elif record_type == "compaction/summary":
        d = {**d, "usage": Usage(**d["usage"])}
    return data_cls(**d)


def _unpack_text_chunk(record: dict) -> list[SessionRecordData]:
    """把一条 text-chunk 打包行解包回 merge 前的多条 assistant/chunk 记录。

    seq 逐位取自 source_event_seqs（权威清单，首条与打包行信封 seq 相同）；
    timestamp 用组首时间 + dt 累积重建；tool-call 片段的 id/name 只还原到
    组内首条，其余片段为 None，与打包时的携带规则一致。
    """
    d = record["data"]
    seqs = record.get("source_event_seqs")
    if not seqs:
        logger.warning("text-chunk(seq=%s)缺少source_event_seqs，无法解包，跳过", record.get("seq"))
        return []
    turn, step = record.get("turn"), record.get("step")
    # 组首 timestamp 即打包行信封时间，dt[0]=0 对应首条
    cur = datetime.datetime.fromisoformat(record["timestamp"])
    chunks = []
    for i, seq in enumerate(seqs):
        if i > 0:
            cur = cur + datetime.timedelta(milliseconds=d["dt"][i])
        if d["type"] == "tool-call":
            chunk = StreamChunk(
                tool_index=d["index"],
                tool_id=d["id"] if i == 0 else None,
                tool_name=d["name"] if i == 0 else None,
                tool_arguments_delta=d["args"][i] if d["args"] else None,
            )
        elif d["type"] == "content":
            chunk = StreamChunk(content=d["texts"][i])
        else:
            chunk = StreamChunk(reasoning_content=d["texts"][i])
        chunks.append(SessionRecordData(
            type="assistant/chunk",
            seq=seq,
            turn=turn,
            step=step,
            uuid=d["uuid"][i],
            timestamp=cur,
            surface_op=None,
            source_event_seqs=None,
            data=AssistantChunkData(chunk),
        ))
    return chunks


def load(session_id: str) -> tuple[SessionMetaData, list[SessionRecordData]] | None:
    """按 session_id 加载会话文件，还原为 meta 与记录列表。

    记录中的 text-chunk 已解包为 assistant/chunk，还原结果与写入前的
    内存形态一致，可直接传给 Session 构造。

    Args:
        session_id: 会话 id，定位 localData/session/{session_id}.jsonl。

    Returns:
        (SessionMetaData, list[SessionRecordData])；文件不存在或为空时
        warning 并返回 None。
    """
    path = local_data_path / "session" / f"{session_id}.jsonl"
    if not path.exists():
        logger.warning("session 文件不存在: %s", path)
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        logger.warning("session 文件为空: %s", path)
        return None

    meta_d = json.loads(lines[0])
    meta = SessionMetaData(
        cwd="",
        session_id=meta_d["session_id"],
        name=meta_d.get("name", ""),
        created_at=datetime.datetime.fromisoformat(meta_d["created_at"]),
        updated_at=datetime.datetime.fromisoformat(meta_d["updated_at"]) if meta_d.get("updated_at") else None,
        format_version=meta_d.get("format_version", 1),
        parent_session_id=meta_d.get("parent_session_id"),
    )

    records: list[SessionRecordData] = []
    for line in lines[1:]:
        record_d = json.loads(line)
        if record_d["type"] == "text-chunk":
            records.extend(_unpack_text_chunk(record_d))
            continue
        data = _restore_data(record_d["type"], record_d["data"])
        if data is None:
            logger.warning("无法还原的记录类型 %s(seq=%s)，跳过", record_d["type"], record_d.get("seq"))
            continue
        records.append(SessionRecordData(
            type=record_d["type"],
            seq=record_d["seq"],
            data=data,
            turn=record_d.get("turn"),
            step=record_d.get("step"),
            uuid=record_d["uuid"],
            timestamp=datetime.datetime.fromisoformat(record_d["timestamp"]),
            surface_op=record_d.get("surface_op"),
            source_event_seqs=record_d.get("source_event_seqs"),
        ))
    return meta, records
