from typing import Literal
from myagent.infra.events.payload import PreAgentLoopPayload,AfterToolUserPayload,AfterLLmCallPayload

class EventSpec:
    """
    事件类型约定
    """
    def __init__(self,name:str,semantics:Literal["waterfall","emit"],payload_type):
        self.name = name
        self.semantics = semantics
        self.payload_type = payload_type

# evnet事件集中定义位置，内容过多时分模块文件导出
pre_agent_loop_event = EventSpec("agent/pre-agent-loop","emit",PreAgentLoopPayload)
after_loop_llm_call = EventSpec("loop/after-llm-call",'emit',AfterLLmCallPayload)
after_tool_use_event = EventSpec("tool/after_tool_use","emit",AfterToolUserPayload) 
