from dataclasses import dataclass
from typing import Literal, TypedDict


@dataclass
class AgentConfig:
    step_limit : int
    max_retry_count : int

@dataclass
class FinalResult:
    reason_type : Literal["success","interrupted","error"]
    reason_text : str  
    error_type : str = ""


class ErrorVerdict(TypedDict, total=False):
    """REQUEST_ERROR waterfall 的裁决契约（loop 边界消费，产出方按此约定构造 dict）。

    total=False：四个键都可缺省。整个 dict 为空 = 无人认领，此时 decision 读作 None
    （loop 据此抛 AgentUnclaimedError）。

    Attributes:
        decision: 裁决动作。retry / backoff_retry 走重试档，continue 走继续档，
            break 终止本 turn。
        policy: 退避参数（base_delay / multiplier / cap），仅 retry / backoff_retry 消费。
        grant: 预算授予意图 {"steps": N}，仅 continue 消费。
        as_error: 终止是否按失败上报，仅 break 消费。
    """
    decision: Literal["retry", "backoff_retry", "continue", "break"]
    policy: dict
    grant: dict
    as_error: bool


