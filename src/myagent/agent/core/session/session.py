from myagent.agent.core.provider import Message
from myagent.agent.core.session.types import (
    SessionMetaData,SessionData,AssistantChunkData,AssistantMessageData, StepEndData,CompactionStartData,
    SessionRecordData,ToolCallData,ToolResultData,TurnEndData,CompactionSummaryData,
    TurnStartData,StepStartData,UserMessageData,RequestHeaderData,CompactionEndData
)
from myagent.infra.events.service import EventService
from myagent.infra.events.eventspec import SESSION_EVENT,SessionEventPayload
from myagent.agent.core.session.surface import SurfaceManager
from myagent.agent.core.session.persistence import SessionPresist
from myagent.config.system_config import local_data_path
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
    }

    # 信封上 step 记为 None 的事件：不属于任何 step（轮边界事件与压缩事务）
    NO_STEP_EVENTS: frozenset[str] = frozenset({
        "turn/start", "turn/end",
        "compaction/start", "compaction/summary", "compaction/end",
    })

    def __init__(self,event_service :EventService,meta_data :SessionMetaData,record_list:list[SessionRecordData] | None =None ,name : str | None = None  ):


        self.meta_data = meta_data
        self.record_list : list[SessionRecordData] = record_list if record_list else []
        self._event_service = event_service
        self.surface_manager = SurfaceManager(record_list)
        self.presistence = SessionPresist(event_service,local_data_path / f"session/{meta_data.session_id}.jsonl", meta_data )
        # 恢复坐标：轮间压缩的记录 turn 为 None，向前找最近一条带 turn 的记录
        self.turn = 0
        for record in reversed(self.record_list):
            if record.turn is not None:
                self.turn = record.turn
                break
        self.step = 0
    
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
        self._event_service.trigger(SESSION_EVENT,SessionEventPayload(copy.deepcopy(record)))

    def compact(self):
        pass 
