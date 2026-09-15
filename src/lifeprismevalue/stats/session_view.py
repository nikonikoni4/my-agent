"""session jsonl -> 结构化视图（离线统计的数据源）。

session 落盘为事件流：每行一个事件，定位信息（type/seq/turn/step/timestamp）在信封上，
载荷在 data 里。本模块把一次 session 解析成 turns / steps / tool_calls 三层结构，
供各统计组件共享读取：只解析一次，避免每个组件重复扫文件。

只做解析，不做任何统计口径判断；组件负责从视图里提取自己关心的字段。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from lifeprismevalue.utils.time_utils import parse_iso_to_aware


@dataclass
class TokenUsage:
    """一次 LLM 调用的 token 用量（对应 assistant/message.data.usage）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ToolCallView:
    """一次工具调用：tool/call 与配对的 tool/result 合并成一条。

    arguments 保留 wire 原样字符串（忠实记录模型输出）；需要结构化时用 arguments_json。
    """

    turn: int
    step: int
    call_id: str
    tool_name: str
    arguments: str = ""
    is_error: bool | None = None
    error_type: str | None = None
    duration_ms: int | None = None
    result_content: str = ""

    @property
    def arguments_json(self) -> Any:
        """把 arguments 解析为对象；不是合法 JSON 时返回 None。"""
        try:
            return json.loads(self.arguments or "{}")
        except (json.JSONDecodeError, TypeError):
            return None


@dataclass
class StepView:
    """一个步骤：一次 LLM 调用 + 执行其请求的工具。"""

    turn: int
    step: int
    start_ts: datetime | None = None
    end_ts: datetime | None = None
    llm_end_ts: datetime | None = None  # assistant/message 落点，近似 LLM 流式结束
    first_chunk_ts: datetime | None = None  # 首个流式片段，用于首 token 延迟
    last_chunk_ts: datetime | None = None
    end_reason_type: str | None = None
    end_reason_text: str = ""
    usage: TokenUsage | None = None
    tool_calls: list[ToolCallView] = field(default_factory=list)


@dataclass
class TurnView:
    """一轮：用户一条消息到助手完整答完。"""

    turn: int
    start_ts: datetime | None = None
    end_ts: datetime | None = None
    end_reason_type: str | None = None
    end_reason_text: str = ""
    steps: list[int] = field(default_factory=list)


@dataclass
class SessionView:
    """一次 session（= 一个任务）的完整结构化视图。"""

    session_id: str
    name: str = ""
    model_name: str = ""
    created_at: str = ""
    updated_at: str = ""
    path: str = ""
    turns: list[TurnView] = field(default_factory=list)
    steps: list[StepView] = field(default_factory=list)
    tool_calls: list[ToolCallView] = field(default_factory=list)


def _extract_text(content: Any) -> str:
    """从消息 content 中提取文本（content 可能是 str 或 block 列表）。"""
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    if content is None:
        return ""
    return content if isinstance(content, str) else str(content)


def _parse_ts(value: str | None) -> datetime | None:
    """解析事件时间戳为 aware datetime；缺失或非法返回 None。"""
    if not value:
        return None
    try:
        return parse_iso_to_aware(value)
    except (ValueError, TypeError):
        return None


def _get_turn(turns: dict[int, TurnView], turn: int | None) -> TurnView:
    """取（或惰性创建）某轮视图，容忍缺失的 turn/start。"""
    key = turn if turn is not None else 0
    view = turns.get(key)
    if view is None:
        view = TurnView(turn=key)
        turns[key] = view
    return view


def _get_step(
    turns: dict[int, TurnView], steps: dict[tuple[int, int], StepView], turn: int | None, step: int | None
) -> StepView | None:
    """取（或惰性创建）某步视图；step 为空（不属于任何 step 的事件）返回 None。"""
    if step is None:
        return None
    turn_key = turn if turn is not None else 0
    key = (turn_key, step)
    view = steps.get(key)
    if view is None:
        view = StepView(turn=turn_key, step=step)
        steps[key] = view
        parent = _get_turn(turns, turn_key)
        if step not in parent.steps:
            parent.steps.append(step)
    return view


def _consume(
    event: dict[str, Any],
    session: SessionView,
    turns: dict[int, TurnView],
    steps: dict[tuple[int, int], StepView],
    calls_by_id: dict[str, ToolCallView],
) -> None:
    """把单条事件归入视图。未知事件类型直接跳过（向前兼容）。"""
    etype = event.get("type")
    data = event.get("data") or {}
    turn = event.get("turn")
    step = event.get("step")
    ts = _parse_ts(event.get("timestamp"))

    if etype == "meta_data":
        # meta 行是"扁平"结构：字段在事件顶层，不在 data 里（见 SessionMetaData.meta_data）
        session.session_id = event.get("session_id") or session.session_id
        session.name = event.get("name") or session.name
        session.created_at = event.get("created_at", "")
        session.updated_at = event.get("updated_at", "")
        return

    if etype == "request/header":
        # 一次请求的配置快照；模型名取第一份（后续变更不影响本 session 归属）
        if not session.model_name:
            session.model_name = data.get("model_name", "")
        return

    if etype == "turn/start":
        _get_turn(turns, turn).start_ts = ts
        return

    if etype == "turn/end":
        view = _get_turn(turns, turn)
        view.end_ts = ts
        view.end_reason_type = data.get("reason_type")
        view.end_reason_text = data.get("reason_text", "")
        return

    if etype == "step/start":
        view = _get_step(turns, steps, turn, step)
        if view is not None:
            view.start_ts = ts
        return

    if etype == "step/end":
        view = _get_step(turns, steps, turn, step)
        if view is not None:
            view.end_ts = ts
            view.end_reason_type = data.get("reason_type")
            view.end_reason_text = data.get("reason_text", "")
        return

    if etype == "assistant/message":
        view = _get_step(turns, steps, turn, step)
        if view is None:
            return
        view.llm_end_ts = ts
        usage = data.get("usage") or {}
        delta = TokenUsage(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
        )
        # 同一步理论上只有一次 LLM 调用；出现多次时累加，避免漏计
        if view.usage is None:
            view.usage = delta
        else:
            view.usage.prompt_tokens += delta.prompt_tokens
            view.usage.completion_tokens += delta.completion_tokens
            view.usage.total_tokens += delta.total_tokens
        return

    if isinstance(etype, str) and "chunk" in etype:
        # 流式片段（text-chunk / assistant/chunk / tool-call-chunks 等），只取时间用于首 token 延迟
        view = _get_step(turns, steps, turn, step)
        if view is None:
            return
        if view.first_chunk_ts is None:
            view.first_chunk_ts = ts
        view.last_chunk_ts = ts
        return

    if etype == "tool/call":
        view = _get_step(turns, steps, turn, step)
        call = ToolCallView(
            turn=turn if turn is not None else 0,
            step=step if step is not None else 0,
            call_id=data.get("call_id", ""),
            tool_name=data.get("tool_name", ""),
            arguments=data.get("arguments", ""),
        )
        if view is not None:
            view.tool_calls.append(call)
        session.tool_calls.append(call)
        if call.call_id:
            calls_by_id[call.call_id] = call
        return

    if etype == "tool/result":
        call = calls_by_id.get(data.get("call_id", ""))
        if call is None:
            # 配不到 tool/call（异常/裁剪）时补一条，保证结果不丢
            call = ToolCallView(
                turn=turn if turn is not None else 0,
                step=step if step is not None else 0,
                call_id=data.get("call_id", ""),
                tool_name=data.get("tool_name", ""),
            )
            session.tool_calls.append(call)
            view = _get_step(turns, steps, turn, step)
            if view is not None:
                view.tool_calls.append(call)
            if call.call_id:
                calls_by_id[call.call_id] = call
        call.is_error = bool(data.get("is_error"))
        call.error_type = data.get("error_type")
        call.duration_ms = data.get("duration_ms")
        message = data.get("message") or {}
        call.result_content = _extract_text(message.get("content"))
        return


def load_session_view(session_path: str | Path) -> SessionView:
    """读取一个 session jsonl 文件，返回结构化视图。

    单行解析失败（坏行）跳过，不影响整体；文件不存在由调用方处理。
    """
    path = Path(session_path)
    session = SessionView(session_id=path.stem, name=path.stem, path=str(path))
    turns: dict[int, TurnView] = {}
    steps: dict[tuple[int, int], StepView] = {}
    calls_by_id: dict[str, ToolCallView] = {}

    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            _consume(event, session, turns, steps, calls_by_id)

    session.turns = sorted(turns.values(), key=lambda t: t.turn)
    session.steps = sorted(steps.values(), key=lambda s: (s.turn, s.step))
    return session
