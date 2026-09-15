"""PathStats 组件：统计一次 session 的工具调用路径。

口径（对应评估目标：路径类）：
- 按时间顺序记录每一次工具调用的 turn/step/名称/参数，供人工与"理想最短路径"比对。
- 统计重复调用、连续重复同一工具、调用总数与涉及工具数，用于识别绕路与反复试错。
不判断对错，只如实呈现路径与参数（对错由人工或后续组件裁决）。
"""

from __future__ import annotations

import json
from typing import Any

from lifeprismevalue.stats.base import StatsComponent
from lifeprismevalue.stats.session_view import SessionView


def _flatten(text: str) -> str:
    """把可能含换行的参数压成单行，便于人工逐行比对。"""
    return " ".join(str(text).split())


def _usage_args(call) -> str:
    """参数的展示文本：优先结构化 JSON，解析失败回退原文。"""
    parsed = call.arguments_json
    if parsed is None:
        return _flatten(call.arguments)
    return json.dumps(parsed, ensure_ascii=False)


class PathStats(StatsComponent):
    """工具调用路径统计组件。"""

    name = "PathStats"

    def analyze(self, session: SessionView) -> dict[str, Any]:
        sequence: list[dict[str, Any]] = []
        tool_counter: dict[str, int] = {}
        error_count = 0
        consecutive_repeats = 0

        prev_name: str | None = None
        for index, call in enumerate(session.tool_calls, start=1):
            tool_counter[call.tool_name] = tool_counter.get(call.tool_name, 0) + 1
            if call.is_error:
                error_count += 1
            if call.tool_name == prev_name:
                consecutive_repeats += 1
            prev_name = call.tool_name

            sequence.append(
                {
                    "index": index,
                    "turn": call.turn,
                    "step": call.step,
                    "tool_name": call.tool_name,
                    "arguments": call.arguments,
                    "arguments_json": call.arguments_json,
                    "is_error": call.is_error,
                    "error_type": call.error_type,
                }
            )

        per_step = [
            {
                "turn": s.turn,
                "step": s.step,
                "tools": [tc.tool_name for tc in s.tool_calls],
            }
            for s in session.steps
        ]

        return {
            "turn_count": len(session.turns),
            "step_count": len(session.steps),
            "tool_call_count": len(session.tool_calls),
            "distinct_tools": sorted(tool_counter),
            "tool_counter": tool_counter,
            "repeated_tools": {name: cnt for name, cnt in tool_counter.items() if cnt > 1},
            "consecutive_repeats": consecutive_repeats,
            "error_call_count": error_count,
            "tool_sequence": sequence,
            "per_step": per_step,
        }

    def render(self, result: dict[str, Any]) -> str:
        lines = [
            "===== PathStats（工具调用路径）=====",
            f"turn 数 {result['turn_count']} | step 数 {result['step_count']}"
            f" | 工具调用 {result['tool_call_count']} | 涉及工具 {len(result['distinct_tools'])}",
            f"失败调用 {result['error_call_count']} | 连续重复调用 {result['consecutive_repeats']}",
            "",
            "工具调用序列（名称 + 参数，供人工比对）：",
        ]

        sequence = result.get("tool_sequence") or []
        if not sequence:
            lines.append("  （本次 session 无工具调用）")
        for item in sequence:
            status = "error" if item["is_error"] else "ok"
            lines.append(
                f"  #{item['index']} [turn{item['turn']}.step{item['step']}] "
                f"{item['tool_name']}  args={_usage_args_by_item(item)}  -> {status}"
            )

        lines.append("")
        lines.append("按 step 汇总：")
        for item in result.get("per_step") or []:
            tools = item["tools"]
            shown = ", ".join(tools) if tools else "(无工具调用)"
            lines.append(f"  turn{item['turn']}.step{item['step']}: {shown}")

        repeated = result.get("repeated_tools") or {}
        if repeated:
            dist = "，".join(f"{name}×{cnt}" for name, cnt in repeated.items())
            lines.append("")
            lines.append(f"重复调用的工具：{dist}")
        return "\n".join(lines)


def _usage_args_by_item(item: dict[str, Any]) -> str:
    """从已序列化的序列项渲染参数文本（保持与 analyze 一致的展示口径）。"""
    parsed = item.get("arguments_json")
    if parsed is None:
        return _flatten(item.get("arguments") or "")
    return json.dumps(parsed, ensure_ascii=False)
