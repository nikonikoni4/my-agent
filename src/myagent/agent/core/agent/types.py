from dataclasses import dataclass
from typing import Literal

from myagent.agent.core.provider import ChatParams
from myagent.agent.core.session.session import Session
from myagent.agent.core.systemprompt import SystemPrompt
from myagent.agent.core.tool import ToolRegister
from myagent.infra.events.service import EventService


@dataclass
class AgentConfig:
    parames : ChatParams
    model : str
    prompt_render_parame : dict
    provider : str  = "custom" # openai格式，目前只支持这个，后续更多服务商修改这个类型并扩展
@dataclass
class AgentContext:
    event_service : EventService
    system_prompt : SystemPrompt
    tool_register : ToolRegister
    session : Session

@dataclass
class StepOut:
    continue_ : bool
    reason_type : Literal["success","interrupted","error"]
    reason_text : str  


