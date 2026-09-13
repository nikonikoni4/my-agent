"""systemprompt 包的对外导出。

- SystemPrompt：提示词管理（隔离注册、同名遮蔽、动态组装）
- PrompSection：一个提示词段（name/order/text）
- ContextType：上下文类型（System Reminder / Runtime）
- ContextItem：一条带类型标注的上下文
- AssemblyPrompt：组装结果（合并后的段 + runtime-context + system-reminder）
"""

from myagent.agent.core.systemprompt.systemprompt import SystemPrompt
from myagent.agent.core.systemprompt.types import (
    AssemblyPrompt,
    ContextItem,
    ContextType,
    PrompSection,
)

__all__ = [
    "SystemPrompt",
    "PrompSection",
    "ContextType",
    "ContextItem",
    "AssemblyPrompt",
]