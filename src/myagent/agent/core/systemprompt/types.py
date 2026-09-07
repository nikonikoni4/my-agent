
from dataclasses import dataclass
from typing import Callable


@dataclass
class PrompSection:
    name : str
    order : int
    text : str | Callable

@dataclass
class AssemblyPrompt:
    sections : dict[str, PrompSection] # name -> section，同名覆盖后的合并结果
    context : str # runtime-context 以及 策略不能去重写稳定的 system prompt 前缀缓存

    def sorted_sections(self)->list[PrompSection]:
        """按 order 升序返回段的快照；不修改自身（排序留给渲染时做）"""
        return sorted(self.sections.values(), key=lambda s : s.order)
