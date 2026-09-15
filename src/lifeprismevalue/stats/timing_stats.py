"""TimingStats 组件：统计一次 session 的耗时结构。

口径（对应评估目标：预算类中的"变慢"）：
- 每 step 总耗时 = step/end - step/start。
- LLM 耗时 ≈ assistant/message - step/start（流式结束落点），首 token 延迟 = 首个流式片段 - step/start。
- 工具耗时 = tool/result.duration_ms，累加。
把每步拆成 LLM 时间与工具时间，回答"慢在模型还是慢在工具"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lifeprismevalue.stats.base import StatsComponent
from lifeprismevalue.stats.session_view import SessionView, StepView


def _delta_ms(start: datetime | None, end: datetime | None) -> int | None:
    """(end - start) 的毫秒数；任一为空返回 None。"""
    if start is None or end is None:
        return None
    return int((end - start).total_seconds() * 1000)


class TimingStats(StatsComponent):
    """耗时维度统计组件。"""

    name = "TimingStats"

    def analyze(self, session: SessionView) -> dict[str, Any]:
        per_step: list[dict[str, Any]] = []
        total_step_ms = 0
        total_llm_ms = 0
        total_tool_ms = 0
        total_first_token_ms = 0
        has_tool_duration = False

        for s in session.steps:
            step_ms = _delta_ms(s.start_ts, s.end_ts)
            llm_ms = _delta_ms(s.start_ts, s.llm_end_ts)
            first_token_ms = _delta_ms(s.start_ts, s.first_chunk_ts)
            # duration_ms 可能整体未采集，此时以 None 表示"无数据"，而非 0
            durations = [tc.duration_ms for tc in s.tool_calls if tc.duration_ms is not None]
            tool_ms = sum(durations) if durations else None

            if step_ms is not None:
                total_step_ms += step_ms
            if llm_ms is not None:
                total_llm_ms += llm_ms
            if tool_ms is not None:
                total_tool_ms += tool_ms
                has_tool_duration = True
            if first_token_ms is not None:
                total_first_token_ms += first_token_ms

            per_step.append(
                {
                    "turn": s.turn,
                    "step": s.step,
                    "step_ms": step_ms,
                    "llm_ms": llm_ms,
                    "tool_ms": tool_ms,
                    "first_token_ms": first_token_ms,
                    "tool_call_count": len(s.tool_calls),
                }
            )

        slowest = sorted(
            [item for item in per_step if item["step_ms"] is not None],
            key=lambda item: item["step_ms"],
            reverse=True,
        )

        return {
            "step_count": len(session.steps),
            "total_step_ms": total_step_ms,
            "total_llm_ms": total_llm_ms,
            # 全 session 都没采集到 duration_ms 时为 None（区别于真实的 0ms）
            "total_tool_ms": total_tool_ms if has_tool_duration else None,
            "total_first_token_ms": total_first_token_ms,
            "per_step": per_step,
            "slowest_steps": slowest[:5],
        }

    def render(self, result: dict[str, Any]) -> str:
        def sec(ms: int | None, unavailable: str = "N/A") -> str:
            return unavailable if ms is None else f"{ms / 1000:.2f}s"

        no_duration = "N/A（session 未记录 duration_ms）"
        lines = [
            "===== TimingStats（耗时结构）=====",
            f"总 step 耗时    : {sec(result['total_step_ms'])}",
            f"其中 LLM 耗时   : {sec(result['total_llm_ms'])}",
            f"其中工具耗时    : {sec(result['total_tool_ms'], no_duration)}",
            "",
            "各 step 明细（step_ms / llm_ms / tool_ms）：",
        ]
        per_step = result.get("per_step") or []
        if not per_step:
            lines.append("  （无 step 数据）")
        for item in per_step:
            lines.append(
                f"  turn{item['turn']}.step{item['step']}  step {sec(item['step_ms'])}"
                f" | llm {sec(item['llm_ms'])} | tool {sec(item['tool_ms'])}"
            )
        return "\n".join(lines)
