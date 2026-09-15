"""TokenStats 组件：统计一次 session 的 token 消耗。

口径（对应评估目标：预算类）：
- 总 token：所有 step 的 usage 求和，一眼看总消耗。
- 首步 prompt token：≈ system prompt + 工具 schema + 用户输入，用于校准与 prompt 大小评估。
- 每 step 的 prompt / completion：分别观察"上下文是否逐步膨胀"和"哪一步思考最久"。
  completion 按 step 排序是最有用的信号：通常首步最大，中段突然增大往往意味着异常。
"""

from __future__ import annotations

from typing import Any

from lifeprismevalue.stats.base import StatsComponent
from lifeprismevalue.stats.session_view import SessionView, StepView, TokenUsage


def _avg(total: int, count: int) -> float:
    return round(total / count, 2) if count else 0.0


def _step_brief(step: StepView) -> dict[str, Any]:
    usage = step.usage or TokenUsage()
    return {
        "turn": step.turn,
        "step": step.step,
        "prompt": usage.prompt_tokens,
        "completion": usage.completion_tokens,
        "total": usage.total_tokens,
    }


class TokenStats(StatsComponent):
    """token 维度统计组件。"""

    name = "TokenStats"

    def analyze(self, session: SessionView) -> dict[str, Any]:
        # 只统计真正带 usage 的 step；缺失 usage 的 step 不参与平均（避免拉低口径）
        steps: list[tuple[StepView, TokenUsage]] = [
            (s, s.usage) for s in session.steps if s.usage is not None
        ]
        total = TokenUsage()
        for _, usage in steps:
            total.prompt_tokens += usage.prompt_tokens
            total.completion_tokens += usage.completion_tokens
            total.total_tokens += usage.total_tokens

        count = len(steps)
        first = steps[0][0] if steps else None
        ranked = sorted(steps, key=lambda pair: pair[1].completion_tokens, reverse=True)
        ranked = [step for step, _ in ranked]
        max_completion = ranked[0] if ranked else None

        return {
            "total": {
                "prompt": total.prompt_tokens,
                "completion": total.completion_tokens,
                "total": total.total_tokens,
            },
            "step_count": count,
            "avg_per_step": {
                "prompt": _avg(total.prompt_tokens, count),
                "completion": _avg(total.completion_tokens, count),
                "total": _avg(total.total_tokens, count),
            },
            # 首步 prompt ≈ system prompt + 工具 schema + 用户输入，用于校准
            "first_step": _step_brief(first) if first else None,
            "max_completion_step": _step_brief(max_completion) if max_completion else None,
            "per_step": [_step_brief(s) for s, _ in steps],
            "completion_ranking": [_step_brief(s) for s in ranked],
        }

    def render(self, result: dict[str, Any]) -> str:
        total = result["total"]
        lines = [
            "===== TokenStats（token 消耗）=====",
            f"总 token        : {total['total']}（prompt {total['prompt']} / completion {total['completion']}）",
            f"step 数         : {result['step_count']}",
            f"平均每 step     : total {result['avg_per_step']['total']}"
            f"（prompt {result['avg_per_step']['prompt']} / completion {result['avg_per_step']['completion']}）",
        ]

        first = result.get("first_step")
        if first:
            lines.append(
                f"首步 token      : prompt {first['prompt']} / completion {first['completion']}"
                f"  <- 校准 system prompt + 工具 schema"
            )

        lines.append("completion 排行（看哪一步思考最久）：")
        ranking = result.get("completion_ranking") or []
        if not ranking:
            lines.append("  （无 usage 数据）")
        for i, item in enumerate(ranking, start=1):
            lines.append(
                f"  {i}. turn{item['turn']}.step{item['step']}  completion {item['completion']}"
                f"（total {item['total']}）"
            )

        per_step = result.get("per_step") or []
        if per_step:
            growth = ", ".join(str(item["prompt"]) for item in per_step)
            lines.append(f"逐步 prompt     : [{growth}]  <- 是否逐步膨胀")
        return "\n".join(lines)
