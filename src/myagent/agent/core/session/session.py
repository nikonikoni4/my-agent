from myagent.agent.core.provider import Message
from myagent.agent.core.session.types import (
    SessionMetaData,SessionData,ToolCallChunksData,AssistantMessageData, StepEndData,CompactionStartData,
    SessionRecordData,ReasoningChunksData,ToolCallData,ToolResultData,TurnEndData,CompactionSummaryData,
    TurnStartData,StepStartData,UserMessageData,ContentChunksData,RequestHeaderData,CompactionEndData
)
from myagent.infra.events.service import EventService
from myagent.infra.events.eventspec import SESSION_EVENT
from myagent.agent.core.session.surface import SurfaceManager
from dataclasses import replace
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
        "content-chunks": ContentChunksData,
        "reasoning-chunks": ReasoningChunksData,
        "tool-call-chunks": ToolCallChunksData,
        "assistant/message": AssistantMessageData,
        "tool/call": ToolCallData,
        "tool/result": ToolResultData,
        "step/end": StepEndData,
        "turn/end": TurnEndData,
        "compaction/start": CompactionStartData,
        "compaction/summary": CompactionSummaryData,
        "compaction/end": CompactionEndData,
    }
    def __init__(self,event_service :EventService,meta_data :SessionMetaData,record_list:list[SessionRecordData] | None =None ,name : str | None = None  ):


        self.meta_data = meta_data
        self.record_list : list[SessionRecordData] = record_list if record_list else []
        self._event_service = event_service
        self.surface_manager = SurfaceManager(record_list)
        self.turn = 0 if not self.record_list else getattr(self.record_list[-1].data, "turn", 0) # 一个正确的session应该最后一条是turn/stop 或 compaction/end 必有turn
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
    
    def _build_record(self,data:SessionData,event_type,surface_op = None,source_event_seqs=None):
        if event_type == "turn/start":
            self.turn += 1
            self.step = 0
        elif event_type == "step/start":
            self.step += 1 

        # 用会话当前坐标覆盖 data 里自带的 turn/step；replace 生成副本，不改调用方传入的原对象
        overrides = {}
        if hasattr(data, "turn"):
            overrides["turn"] = self.turn
        if hasattr(data, "step"):
            overrides["step"] = self.step
        if overrides:
            data = replace(data, **overrides)

        
        return SessionRecordData(
            type = event_type,
            seq = self.record_list_seq + 1,
            surface_op = surface_op,
            source_event_seqs = source_event_seqs,
            data = data
        )

    def append(self,event_type:str,data:SessionData,surface_op,source_event_seqs):
        if event_type not in self.RECORD_DATA_TYPES:
            raise ValueError(f"{event_type} 不属于session 记录范围")
        if not isinstance(data,self.RECORD_DATA_TYPES[event_type]):
            raise TypeError(f"传入的数据类型不符合要求,应该为:{self.RECORD_DATA_TYPES[event_type].__name__}")
        self.record_list.append(self._build_record(data,event_type,surface_op,source_event_seqs))
        self.surface_manager.refresh_node(self.record_list)
        #  self._event_service.trigger(SESSION_EVENT,None)

    def compact(self):
        pass 