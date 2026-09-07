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
from pathlib import Path

from myagent.agent.core.session.session import Session
from myagent.agent.core.session.persistence import SessionPresist
from myagent.agent.core.session.types import SessionMetaData, SessionRecordData, AssistantChunkData
from myagent.agent.core.provider import Message, ToolCallRequest, ChatParams, Usage, StreamChunk
from myagent.infra.events import EventService
from myagent.utils.helper import project_path_to_session_folder

logger = logging.getLogger(__name__)


class SessionStore:
    """
    作用：负责 session 的管理(load/resume，create，fork)
    session 的存储方法：应该是一个可扩展的存储方式，目前只支持从文件获取，但是要能够实现可扩展性，比如未来支持sqlite存储
    """

    def __init__(self, session_folder: Path,event_service:EventService):
        self.session_folder = session_folder
        self._event_service = event_service

    def _session_file(self, session_id: str, project_path: Path) -> Path:
        """按 session_id + project_path 定位会话文件：项目路径编码为 session_folder 下的项目子文件夹"""
        return project_path_to_session_folder(project_path, self.session_folder) / f"{session_id}.jsonl"

    def create(self, name: str, project_path: Path) -> Session:
        """新建一个空会话，返回组装好的 Session。

        SessionPresist 在构造时订阅 session/event，后续 session.append 触发
        事件后由它异步落盘；meta 行随首批记录自动写入。

        Args:
            name: 会话名称，为空串时 SessionMetaData 自动回退为 session_id。
            project_path: 项目路径，决定会话文件所在的项目子文件夹，并记入 meta 的 cwd。
        """
        meta = SessionMetaData(cwd=str(project_path), name=name)
        session_file = self._session_file(meta.session_id, project_path)
        presist = SessionPresist(self._event_service, session_file, meta)
        return Session(self._event_service, meta, [], presistence=presist)

    def load(self, session_id: str, project_path: Path) -> Session | None:
        """按 session_id + project_path 加载会话，直接返回可用的 Session。

        文件内容还原为 meta 与记录列表后组装 Session（含持久化组件），
        恢复结果与写入前的内存形态一致，可直接传入 Loop 开启对话。

        Args:
            session_id: 会话 id，定位 {project_path 编码后的项目文件夹}/{session_id}.jsonl。
            project_path: 项目路径，编码为 session_folder 下的项目子文件夹。

        Returns:
            组装好的 Session；文件不存在或为空时 warning 并返回 None。
        """
        session_file = self._session_file(session_id, project_path)
        if not session_file.exists():
            logger.warning("session 文件不存在: %s", session_file)
            return None
        lines = session_file.read_text(encoding="utf-8").splitlines()
        if not lines:
            logger.warning("session 文件为空: %s", session_file)
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
                records.extend(self._unpack_text_chunk(record_d))
                continue
            data = self._restore_data(record_d["type"], record_d["data"])
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
        presist = SessionPresist(self._event_service, session_file, meta)
        return Session(self._event_service, meta, records, presistence=presist)

    def fork(self, fork_from_session_id):
        """暂时不实现"""
        pass

    @staticmethod
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

    @staticmethod
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
            d = {**d, "message": SessionStore._restore_message(d["message"])}
        elif record_type == "request/header":
            d = {**d, "params": ChatParams(**d["params"]) if d["params"] else None}
        elif record_type == "compaction/summary":
            d = {**d, "usage": Usage(**d["usage"])}
        return data_cls(**d)

    @staticmethod
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
