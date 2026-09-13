
from dataclasses import dataclass
from enum import Enum
from typing import Callable


@dataclass
class PrompSection:
    name : str
    order : int
    text : str | Callable


class ContextType(str, Enum):
    """Agent 上下文条目的类型。

    - SYSTEM_REMINDER：用户的 System Reminder，来源于文件读取（也允许在系统中
      动态添加）。在 Message List 中固定排在 System Prompt 之后、正常对话之前，
      即第二位；相对稳定，不应被运行时策略覆写。
    - RUNTIME：运行过程中动态生成的标注（如当前时间、终端位置等），不进 System
      Prompt（避免破坏前缀缓存），会合并进当前传入消息的 UserMessage 中。
    """

    SYSTEM_REMINDER = "system_reminder"
    RUNTIME = "runtime"


@dataclass
class ContextItem:
    """一条带类型标注的上下文。

    Attributes:
        text: 上下文文本内容。
        context_type: 上下文类型（System Reminder / Runtime）。
    """

    text: str
    context_type: ContextType


@dataclass
class AssemblyPrompt:
    sections : dict[str, PrompSection] # name -> section，同名覆盖后的合并结果
    context : str # runtime-context，合并进当前 UserMessage；策略不能去重写稳定的 system prompt 前缀缓存
    system_reminder : str # system-reminder，作为 Message List 第二位（System Prompt 之后）

    def sorted_sections(self)->list[PrompSection]:
        """按 order 升序返回段的快照；不修改自身（排序留给渲染时做）"""
        return sorted(self.sections.values(), key=lambda s : s.order)