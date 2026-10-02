from myagent.agent.core.provider import Message
from myagent.agent.core.session.types import (
    SessionMetaData,SessionData,AssistantChunkData,AssistantMessageData, StepEndData,CompactionStartData,
    SessionRecordData,ToolCallData,ToolResultData,TurnEndData,CompactionSummaryData,
    TurnStartData,StepStartData,UserMessageData,RequestHeaderData,CompactionEndData,LLMRetryData,
    AgentGrantData,AgentErrorHandleData
)
from myagent.agent.execption import SessionReBindError
from myagent.infra.events.service import EventService
from myagent.infra.events.eventspec import SESSION_EVENT,SessionEventPayload
from myagent.agent.core.session.surface import SurfaceManager
from myagent.agent.core.session.persistence import SessionPresist
from pathlib import Path
import copy


class Session:
    """
    
    不负责加载时的检查，只要是能够初始化都是正确的session
    """

    # type 标签 -> Data 类的唯一登记处：
    # 写入边界用它校验标签合法，读取边界用它还原并逐字段检查，字段不符抛异常不静默填默认值
    RECORD_DATA_TYPES: dict[str, type] = {
        "turn/start": TurnStartData,
        "step/start": StepStartData,
        "request/header": RequestHeaderData,
        "user/message": UserMessageData,
        "assistant/chunk": AssistantChunkData,
        "assistant/message": AssistantMessageData,
        "tool/call": ToolCallData,
        "tool/result": ToolResultData,
        "step/end": StepEndData,
        "turn/end": TurnEndData,
        "compaction/start": CompactionStartData,
        "compaction/summary": CompactionSummaryData,
        "compaction/end": CompactionEndData,
        "llm/retry" : LLMRetryData,
        "agent/grant": AgentGrantData,
        "agent/error-handle": AgentErrorHandleData
    }

    # 信封上 step 记为 None 的事件：不属于任何 step（轮边界事件与压缩事务）
    NO_STEP_EVENTS: frozenset[str] = frozenset({
        "turn/start", "turn/end",
        "compaction/start", "compaction/summary", "compaction/end",
    })

    def __init__(self,meta_data :SessionMetaData,record_list:list[SessionRecordData] | None =None ,session_file : Path | None = None  ):


        self.meta_data = meta_data
        self.record_list : list[SessionRecordData] = record_list if record_list else []
        self._event_service: EventService | None = None
        self.surface_manager = SurfaceManager(record_list)
        # 构造只恢复数据；bind 时才接入事件并启动持久化。
        # None 路径表示内存态会话，绑定后也不创建持久化组件。
        self._session_file = session_file
        self.presistence: SessionPresist | None = None
        # 构造时已有的记录来自加载，不重复落盘；未绑定期间追加的记录首次绑定时补入缓冲。
        self._initial_record_count = len(self.record_list)
        # 恢复坐标：轮间压缩的记录 turn 为 None，向前找最近一条带 turn 的记录
        self.turn = 0
        for record in reversed(self.record_list):
            if record.turn is not None:
                self.turn = record.turn
                break
        self.step = 0

    def bind(self, event_service: EventService) -> None:
        """一次性绑定运行依赖，并在有文件路径时创建持久化组件。

        Args:
            event_service: 当前 agent 的事件总线。

        绑定是一次性的：已绑定的 Session 拒绝再次绑定（包括同一总线），
        原总线、持久化组件与后台任务一律保留不动，调用方重新装配即得此异常。
        文件态会话必须在运行中的事件循环内绑定；未绑定时可同步读取和追加数据。
        绑定前新增记录补入持久化缓冲，加载的历史记录不重写，也不补发历史事件。

        Raises:
            SessionReBindError: 本 Session 已绑定过总线。
        """
        if self._event_service is not None:
            raise SessionReBindError(f"Session {self.meta_data.session_id} 已绑定，拒绝重复绑定")


        persistence = None
        if self._session_file is not None:
            persistence = SessionPresist(self._session_file, self.meta_data)
            for record in self.record_list[self._initial_record_count:]:
                persistence.cache_data(copy.deepcopy(record))
        self.presistence = persistence
        self._event_service = event_service

    @property
    def record_list_seq(self):
        return len(self.record_list)


    def derive_messages(self)->list[Message]:
        """
        从session日志中派发出
        """
        messages =[]
        node = self.surface_manager.refresh_node(self.record_list)
        if node and self.record_list:
            for seq in node:
                messages.append(self.record_list[seq - 1].data.message)
        return messages
    
    def latest_request_header(self) -> RequestHeaderData | None:
        """返回最近一条 request/header 的配置快照（含 model_name/system_prompt/tools/params）。

        从后往前找最近一条 request/header 记录的 data；本会话从未写入过（新会话首轮）时返回 None。
        """
        for record in reversed(self.record_list):
            if record.type == "request/header":
                return record.data
        return None

    def llm_retry_count(self, turn: int) -> int:
        """统计指定 turn 内 type 为 llm/retry 的记录条数。

        该 turn 不存在或没有重试记录时返回 0；llm/retry 属于 step 内事件，
        信封上带 turn 定位，直接按 turn 过滤即可。
        """
        return sum(
            1
            for record in self.record_list
            if record.turn == turn and record.type == "llm/retry"
        )

    def granted_steps(self, turn: int) -> int:
        """统计指定 turn 内授予的额外步数预算合计。

        该 turn 不存在或没有授予记录时返回 0。与 llm_retry_count 同构——两者都是
        "状态折叠自账本"：预算不持存在 loop 里，判上限时现算（配置基准 + 本收益）。
        带 turn 参数，故查询非本次 turn 也成立。
        """
        return sum(
            record.data.steps
            for record in self.record_list
            if record.turn == turn and record.type == "agent/grant"
        )

    def error_handles(self, turn: int) -> list[AgentErrorHandleData]:
        """按 turn 拉取 agent/error-handle 的 data 列表，保持落盘顺序。

        顺序是本接口契约的一部分：turn 的终态取自**最后一条**（前面的历史处置不该
        影响这一轮怎么收场），所以调用方要靠"最后一条是最新的"这一点定位。

        与 granted_steps / llm_retry_count 同源——都是从账本事实折叠出运行态信息，
        只是这里要整段历史而非一个聚合值。该 turn 无处置记录时返回空列表。
        """
        return [
            record.data
            for record in self.record_list
            if record.turn == turn and record.type == "agent/error-handle"
        ]

    def _build_record(self,data:SessionData,event_type,surface_op = None,source_event_seqs:list |None=None)->SessionRecordData:
        if event_type == "turn/start":
            self.turn += 1
            self.step = 0
        elif event_type == "step/start":
            self.step += 1 
        # 计算source_event_seqs
        elif event_type == "assistant/message":
            for record in reversed(self.record_list):
                if record.type == "assistant/chunk":
                    source_event_seqs.append(record.seq)
                else:
                    break
        elif event_type == "tool/result":
            for record in reversed(self.record_list):
                if record.type == "tool/call":
                    source_event_seqs.append(record.seq)
                else:
                    break
        # turn/step 是定位信息，统一记在信封上；轮间压缩（compact 未实现）届时需显式传 turn=None
        return SessionRecordData(
            type = event_type,
            seq = self.record_list_seq + 1,
            turn = self.turn,
            step = None if event_type in self.NO_STEP_EVENTS else self.step,
            surface_op = surface_op,
            source_event_seqs = source_event_seqs,
            data = data
        )

    def append(self,event_type:str,data:SessionData,surface_op = None,source_event_seqs = None):
        if event_type not in self.RECORD_DATA_TYPES:
            raise ValueError(f"{event_type} 不属于session 记录范围")
        if not isinstance(data,self.RECORD_DATA_TYPES[event_type]):
            raise TypeError(f"传入的数据类型不符合要求,应该为:{self.RECORD_DATA_TYPES[event_type].__name__}")
        # TODO node不处理surface_op
        record = self._build_record(data,event_type,surface_op,source_event_seqs)
        self.record_list.append(record)
        self.surface_manager.refresh_node(self.record_list)
        # 快照一份：缓冲与广播共用同一对象，与改造前"emit 里 deepcopy 一次"的行为一致
        snapshot = copy.deepcopy(record)
        # 落盘：直连持久化组件，不经事件总线——总线是进程内共享的，经它中转会把
        # 本会话的记录灌进同总线上其他会话的缓冲（见 docs/known-limitations
        # 2026-09-11-event-service无隔离机制.md 第 1 节）
        if self.presistence is not None:
            self.presistence.cache_data(snapshot)
        # 观测：只有显式绑定后才广播，不补发绑定前的记录。
        if self._event_service is not None:
            self._event_service.emit(SESSION_EVENT.name,SessionEventPayload(snapshot))

    def compact(self):
        pass 
