"""systemprompt 包的对外导出。

- SystemPrompt：提示词管理（隔离注册、同名遮蔽、动态组装）
- PrompSection：一个提示词段（name/order/text）
- AssemblyPrompt：组装结果（合并后的段 + runtime-context）
"""

from myagent.agent.core.systemprompt.systemprompt import SystemPrompt
from myagent.agent.core.systemprompt.types import PrompSection, AssemblyPrompt

__all__ = [
    "SystemPrompt",
    "PrompSection",
    "AssemblyPrompt",
]
