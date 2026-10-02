from dataclasses import dataclass
from typing import Literal


@dataclass
class AgentConfig:
    step_limit : int
    max_retry_count : int

@dataclass
class FinalResult:
    reason_type : Literal["success","interrupted","error"]
    reason_text : str  
    error_type : str = ""


