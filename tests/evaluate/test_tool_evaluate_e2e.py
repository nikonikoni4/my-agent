"""ToolEvaluate 端到端测试（Mock 版 OpenAI Provider，不发真实网络请求）。

走完整链路：用户消息 -> ReActAgentLoop -> OpenAIProvider.stream_chat（SDK client
被 mock 成按脚本返回的假流）-> 真实 ToolRegister 解析/校验/执行/分类 ->
tool/result 事件 -> ToolEvaluate 落盘 CSV -> 按日期读取与统计。

Mock 的模型输出覆盖当前**全部 7 种**工具错误分类（ToolErrorType）各至少一次，
外加 2 次成功调用，验证"每一种错误信息都能被评估模块正确分类记录"。

与 test_loop_e2e.py 的区别：那个用真实 ARK API（需要凭证、消耗 token），
本文件用 mock provider，因此**不标 e2e 标记**、默认全量运行即执行。
"""

import asyncio
import csv
import json
from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from myagent.agent.core.agent.loop import ReActAgentLoop
from myagent.agent.core.agent.types import AgentConfig
from myagent.agent.core.provider import ChatParams
from myagent.agent.core.session.store import SessionStore
from myagent.agent.core.systemprompt.systemprompt import SystemPrompt
from myagent.agent.core.systemprompt.types import PrompSection
from myagent.agent.core.tool.tool import Tool, ToolErrorType
from myagent.agent.llm.openai_provider import OpenAIProvider
from myagent.evaluate import ToolEvaluate
from myagent.infra.events.eventspec import TURN_END
from myagent.infra.events.service import EventService

AGENT_NAME = "eval_mock"
STEP_LIMIT = 20
PROMPT = "请依次触发各种工具调用场景"


# ---------------------------------------------------------------------------
# Mock OpenAI Provider：把 SDK 流换成预置 chunk 序列
# ---------------------------------------------------------------------------


class FakeStream:
    """把一组预置 chunk 当作 SDK 异步流返回（供 stream_chat 的 async for 消费）。"""

    def __init__(self, chunks: list):
        self._chunks = chunks

    def __aiter__(self):
        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()


class MockStreamScript:
    """按脚本逐次返回一个假流；脚本耗尽即报错（loop 不应再多调模型）。"""

    def __init__(self, rounds: list[list]):
        self._rounds = list(rounds)
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        if not self._rounds:
            raise AssertionError("Mock OpenAI 脚本耗尽：loop 仍在请求模型")
        return FakeStream(self._rounds.pop(0))


def build_mock_provider(rounds: list[list]):
    """构造 OpenAIProvider，并把内部 SDK client 换成按脚本返回的 AsyncMock。"""
    provider = OpenAIProvider(
        model="mock-model",
        api_key="mock-key",
        base_url="https://mock.local/v1",
        chat_params=ChatParams(temperature=0.5),
    )
    script = MockStreamScript(rounds)
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=script)))
    )
    return provider, script


def _delta(content=None, reasoning=None, tool_calls=None):
    return SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)


def _chunk(delta=None, finish_reason=None, usage=None):
    choices = [] if delta is None else [SimpleNamespace(delta=delta, finish_reason=finish_reason)]
    return SimpleNamespace(choices=choices, usage=usage)


def _tool_frag(index, call_id, name, arguments):
    return SimpleNamespace(index=index, id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def tool_round(call_id: str, name: str, arguments: str, finish_reason: str = "tool_calls") -> list:
    """一轮"模型发起工具调用"的假流（finish_reason=length 会被 provider 打成 truncated）。"""
    return [
        _chunk(delta=_delta(tool_calls=[_tool_frag(0, call_id, name, arguments)])),
        _chunk(delta=_delta(), finish_reason=finish_reason),
    ]


def text_round(content: str) -> list:
    """一轮"模型只回文本、不再调工具"的假流（ReAct 终止条件）。"""
    return [
        _chunk(delta=_delta(content=content)),
        _chunk(delta=_delta(), finish_reason="stop"),
    ]


# ---------------------------------------------------------------------------
# 工具：覆盖成功路径与三种执行失败来源
# ---------------------------------------------------------------------------


class TimeTool(Tool):
    """无参数工具，恒定成功。"""

    @property
    def name(self) -> str:
        return "get_time"

    @property
    def description(self) -> str:
        return "获取当前时间"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return "12:00"


class WeatherTool(Tool):
    """有必填参数的工具，用于成功与参数校验失败。"""

    @property
    def name(self) -> str:
        return "get_weather"

    @property
    def description(self) -> str:
        return "查询城市天气"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        }

    async def execute(self, **kwargs) -> str:
        return f"{kwargs.get('city')}晴"


class BoomTool(Tool):
    """执行即抛异常的工具，触发 TOOL_EXECUTION。"""

    @property
    def name(self) -> str:
        return "boom"

    @property
    def description(self) -> str:
        return "总是抛异常的工具"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        raise RuntimeError("工具内部炸了")


class FlakyInterceptTool(Tool):
    """连续失败达阈值后走 execute_intercept 熔断的工具，触发 BREAKER_INTERCEPT。"""

    def __init__(self):
        super().__init__(max_consecutive_failures=5, breaker_mode="execute_intercept", raise_on_break=False)

    @property
    def name(self) -> str:
        return "flaky_intercept"

    @property
    def description(self) -> str:
        return "连续失败会被熔断拦截的工具"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        raise RuntimeError("总是失败")


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


class LoopFixture:
    def __init__(self, tmp_path):
        self.event_service = EventService()
        self.store = SessionStore(tmp_path / "sessions", self.event_service)
        self.session = self.store.create("eval-mock-会话", tmp_path / "proj")

        system_prompt = SystemPrompt()
        system_prompt.register_section(AGENT_NAME, PrompSection(
            name="tool_guide",
            order=50,
            text="按用户要求调用工具。",
        ))

        provider, script = build_mock_provider(build_script())
        self.provider = provider
        self.script = script
        config = AgentConfig(step_limit=STEP_LIMIT, max_retry_count=0)
        self.loop = ReActAgentLoop(
            self.event_service, self.session, system_prompt, config, provider,
            name=AGENT_NAME, prompt_render_parame={},
        )
        self.loop.tool_register.register([TimeTool(), WeatherTool(), BoomTool(), FlakyInterceptTool()])

        self.evaluate_dir = tmp_path / "evaluate"
        self.evaluate = ToolEvaluate(self.event_service, output_dir=self.evaluate_dir)

    async def run_turn(self, prompt: str) -> None:
        """发一条消息并等到 turn 结束（loop 在后台 task 里跑，需等 TURN_END）。"""
        done = asyncio.Event()

        def on_turn_end(_payload):
            done.set()

        # EventService 弱引用持有回调：具名局部函数在测试期间存活，弱引用有效
        self.event_service.register(TURN_END.name, on_turn_end)
        await self.loop.send(prompt, "next_turn")
        await asyncio.wait_for(done.wait(), timeout=10)


def build_script() -> list[list]:
    """模型脚本：2 次成功 + 7 种工具错误分类各至少一次（14 次工具调用）。"""
    return [
        # -- 成功路径 --
        tool_round("c1", "get_time", "{}"),
        tool_round("c2", "get_weather", '{"city": "北京"}'),
        # -- 7 种错误分类 --
        tool_round("c3", "ghost_tool", "{}"),                                   # TOOL_NOT_FOUND
        tool_round("c4", "get_weather", '{"city": "北京"'),                      # PARSE_ERROR
        tool_round("c5", "get_weather", '{"city": "北京', finish_reason="length"),  # PARSE_TRUNCATED
        tool_round("c6", "get_weather", "[1, 2]"),                              # PARSE_NOT_OBJECT
        tool_round("c7", "get_weather", "{}"),                                  # PARAM_VALIDATION
        tool_round("c8", "boom", "{}"),                                         # TOOL_EXECUTION
        # flaky_intercept 连续失败 5 次触发 execute_intercept 熔断，第 6 次被拦截
        *[tool_round(f"c9_{i}", "flaky_intercept", "{}") for i in range(5)],
        tool_round("c14", "flaky_intercept", "{}"),                             # BREAKER_INTERCEPT
        # -- 收敛 --
        text_round("已按预期完成各类工具调用"),
    ]


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_端到端_全链路覆盖每种工具错误并统计(tmp_path):
    fx = LoopFixture(tmp_path)

    await fx.run_turn(PROMPT)
    fx.session.presistence.presist()  # 收尾：把 buffer 里剩余记录落盘

    # ---- 1. 模型按脚本请求了 15 次（14 次工具 + 1 次收敛）----
    assert fx.script.calls == 15

    # ---- 2. 评估模块拿到 14 条记录，与 session 落盘的 tool/result 一一对应 ----
    records = fx.evaluate.read_all()
    session_results = [r for r in fx.session.record_list if r.type == "tool/result"]
    assert len(records) == 14
    assert len(session_results) == 14, "评估订阅不应漏记/多记 tool/result"

    # ---- 3. 每种错误分类至少出现一次（覆盖 ToolErrorType 全集）----
    seen_error_types = {r.error_type for r in records if r.is_error}
    assert seen_error_types == {t.value for t in ToolErrorType}
    # 成功记录 2 条且 error_type 为 None
    successes = [r for r in records if not r.is_error]
    assert {r.name for r in successes} == {"get_time", "get_weather"}
    assert all(r.error_type is None for r in successes)
    # 失败记录都带非空 content（回喂模型的错误详情）
    assert all(r.content for r in records if r.is_error)

    # ---- 4. argument 始终是 JSON 对象（坏 JSON 走 _raw 兜底）----
    assert all(isinstance(r.argument, dict) for r in records)
    parse_error_rec = next(r for r in records if r.error_type == ToolErrorType.PARSE_ERROR.value)
    assert parse_error_rec.argument == {"_raw": '{"city": "北京"'}
    assert parse_error_rec.name == "get_weather"

    # ---- 5. CSV 按日期命名落盘且逐行可读 ----
    today = date.today().strftime("%Y-%m-%d")
    csv_file = fx.evaluate_dir / f"{today}-tool_result_stats.csv"
    assert csv_file.exists()
    with csv_file.open(encoding="utf-8") as f:
        csv_rows = list(csv.DictReader(f))
    assert len(csv_rows) == 14
    assert json.loads(csv_rows[0]["argument"]) == {}

    # ---- 6. 工具维度统计 ----
    stats = fx.evaluate.stats(today, today)
    assert (stats.total, stats.success, stats.failure) == (14, 2, 12)
    assert stats.accuracy == pytest.approx(2 / 14)

    per_tool = {t.name: t for t in stats.tools}
    assert (per_tool["get_time"].total, per_tool["get_time"].success) == (1, 1)
    assert (per_tool["get_weather"].total, per_tool["get_weather"].failure) == (5, 4)
    assert per_tool["get_weather"].error_type_distribution == {
        "parse_error": 1,
        "parse_truncated": 1,
        "parse_not_object": 1,
        "param_validation": 1,
    }
    assert per_tool["ghost_tool"].error_type_distribution == {"tool_not_found": 1}
    assert per_tool["boom"].error_type_distribution == {"tool_execution": 1}
    assert per_tool["flaky_intercept"].error_type_distribution == {
        "tool_execution": 5,
        "breaker_intercept": 1,
    }

    # ---- 7. 报告可渲染 ----
    report = fx.evaluate.format_report(stats)
    assert "总调用次数 : 14" in report
    assert "breaker_intercept:1" in report


@pytest.mark.asyncio
async def test_端到端_评估订阅与session共用总线互不干扰(tmp_path):
    """ToolEvaluate 与 SessionPresist 订阅同一条事件总线：评估记录数 == session 记录数。"""
    fx = LoopFixture(tmp_path)

    await fx.run_turn(PROMPT)
    fx.session.presistence.presist()

    session_results = [r for r in fx.session.record_list if r.type == "tool/result"]
    # 评估器读盘记录与 session 落盘记录在数量、工具名、失败状态上完全对齐
    disk_records = fx.evaluate.read_all()
    assert len(disk_records) == len(session_results)
    assert [r.name for r in disk_records] == [r.data.tool_name for r in session_results]
    assert [r.is_error for r in disk_records] == [r.data.is_error for r in session_results]
