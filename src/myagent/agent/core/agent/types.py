from dataclasses import dataclass
from typing import Literal

from myagent.agent.core.provider import ChatParams
from myagent.agent.core.session.session import Session
from myagent.agent.core.systemprompt import SystemPrompt
from myagent.agent.core.tool.register import ToolRegister
from myagent.infra.events.service import EventService


@dataclass
class AgentConfig:
    step_limit : int 
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


