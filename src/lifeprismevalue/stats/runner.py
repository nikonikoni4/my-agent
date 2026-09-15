"""统计组件 Runner：注册、遍历执行、合并输出。

把「解耦的组件」收敛成一个统一入口：一次 session 只解析一次成 SessionView，
按注册顺序依次执行各组件，合并成单一结构；单个组件抛错被隔离，不影响其它组件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from lifeprismevalue.stats.base import StatsComponent
from lifeprismevalue.stats.path_stats import PathStats
from lifeprismevalue.stats.session_view import SessionView, load_session_view
from lifeprismevalue.stats.timing_stats import TimingStats
from lifeprismevalue.stats.token_stats import TokenStats


def default_components() -> list[StatsComponent]:
    """默认启用全部已实现组件。新增组件在此登记（或在构造 Runner 时自定义）。"""
    return [TokenStats(), TimingStats(), PathStats()]


class StatsRunner:
    """按组件注册顺序执行统计并合并输出。"""

    def __init__(self, components: Sequence[StatsComponent] | None = None):
        """
        Args:
            components: 参与统计的组件列表；None 表示使用默认组件集。
        """
        self.components: list[StatsComponent] = list(components) if components is not None else default_components()

    def run(self, session: SessionView) -> dict[str, Any]:
        """对给定 session 视图执行全部组件，返回合并结果。"""
        components: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for component in self.components:
            # 组件隔离：单个组件失败只记录错误，不阻断其它组件
            try:
                components[component.name] = component.analyze(session)
            except Exception as exc:  # noqa: BLE001 - 有意兜底，保证部分结果可用
                errors[component.name] = f"{type(exc).__name__}: {exc}"
        return {
            "session_id": session.session_id,
            "session_name": session.name,
            "model_name": session.model_name,
            "session_path": session.path,
            "components": components,
            "errors": errors,
        }

    def run_path(self, session_path: str | Path) -> dict[str, Any]:
        """读取 session jsonl 文件并执行统计。"""
        return self.run(load_session_view(session_path))

    def render(self, merged: dict[str, Any]) -> str:
        """把合并结果渲染为可读文本报告。"""
        lines = [
            f"session : {merged['session_id']}（{merged.get('session_name') or ''}）",
            f"model   : {merged.get('model_name') or '(未知)'}",
            f"path    : {merged.get('session_path') or ''}",
            "",
        ]
        for component in self.components:
            result = merged["components"].get(component.name)
            if result is None:
                continue
            lines.append(component.render(result))
            lines.append("")
        for name, message in merged.get("errors", {}).items():
            lines.append(f"[{name}] 组件执行失败: {message}")
        return "\n".join(lines).rstrip() + "\n"


def analyze_session(
    session_path: str | Path, components: Sequence[StatsComponent] | None = None
) -> dict[str, Any]:
    """便捷入口：一次性统计一个 session 文件，返回合并结果。"""
    return StatsRunner(components).run_path(session_path)
