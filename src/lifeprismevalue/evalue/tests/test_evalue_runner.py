"""runner 已实现部分（到 `_run_under_test` / t_end 为止）的测试。

覆盖：`_now` / `_prepare_run` / `_reset_state` / `_apply_precondition` / `_snapshot_case` /
`_write_summary` / `_run_under_test`（scripted 注入、收尾、agent 模式与 trigger 的未实现分支）/
`run` 的兜底与汇总。

被测评 agent 用 `FakeAgent` 注入（`agent_factory`），避免真实 LLM 调用。
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from myagent.agent.core.provider import Message
from myagent.agent.core.session.types import (
    AssistantMessageData,
    SessionRecordData,
    TurnEndData,
)
from myagent.infra.events import EventService
from myagent.infra.events.eventspec import SESSION_EVENT
from myagent.infra.events.payload import SessionEventPayload

from lifeprismevalue.evalue.caseload import load_case_set
from lifeprismevalue.evalue.runner import (
    RULE_SECTION_TITLE,
    SUMMARY_COLUMNS,
    CaseResult,
    EvalRunner,
    TurnCollector,
    _upsert_rule_section,
)

DB_REL = "dataset/lifewatch_ai.db"
CP_REL = "agent/chat/custom_prompt.md"
BASE_CP_TEXT = "# 自定义记录规则\n\n### 支出记录规则\n- 支出类型必须从枚举里选\n"


# ---------------- 夹具 ----------------


def make_base(tmp_path: Path) -> Path:
    """造一个极小的 base 底座（只需包含可变路径 + 一点只读内容）。"""
    base = tmp_path / "base"
    (base / "dataset").mkdir(parents=True)
    (base / "agent" / "chat").mkdir(parents=True)
    (base / DB_REL).write_bytes(b"DB-BASELINE")
    (base / CP_REL).write_text(BASE_CP_TEXT, encoding="utf-8")
    (base / "user" / "user.md").parent.mkdir(parents=True)
    (base / "user" / "user.md").write_text("只读内容", encoding="utf-8")
    return base


def make_runner(tmp_path: Path, base: Path, **kwargs) -> EvalRunner:
    return EvalRunner(base_dir=base, runs_dir=tmp_path / "runs", **kwargs)


def write_cases(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "cases.yaml"
    p.write_text(body, encoding="utf-8")
    return p


CASES_MIN = """
meta:
  id: 记录任务-99
  dataset_version: 3
cases:
  - id: T-1
    type: 支出记录
    evidence: [custom_expense_log]
    turns:
      - role: user
        text: 记录午饭，15.3元
    rubric: 应新增一条支出记录：金额 15.3、类型「餐饮」
"""

CASES_WITH_RULES = """
meta:
  id: 记录任务-98
cases:
  - id: R-1
    type: 锻炼记录
    precondition: {rules: ["锻炼->每日锻炼"]}
    evidence: [custom_exercise_log]
    turns:
      - role: user
        text: 记录锻炼30分钟
    rubric: 应同时打卡
"""

CASES_TWO_TURNS = """
meta:
  id: 记录任务-97
  multi_turn: true
cases:
  - id: M-1
    type: 支出记录
    evidence: [custom_expense_log]
    turns:
      - role: user
        text: 记录午饭
      - role: user
        text: 15.3元
    rubric: 应记录
"""

CASES_AGENT_MODE = """
meta:
  id: 记录任务-96
  multi_turn: true
cases:
  - id: A-1
    type: 锻炼记录
    input_mode: agent
    simulator: {goal: 让 agent 记录一次锻炼}
    evidence: [custom_exercise_log]
    rubric: 应记录
"""

CASES_TRIGGER = """
meta:
  id: 记录任务-95
  multi_turn: true
cases:
  - id: G-1
    type: 支出记录
    evidence: [custom_expense_log]
    turns:
      - role: user
        text: 记录午饭
      - role: user
        text: 确认
        trigger: 询问是否确认
    rubric: 应记录
"""

CASES_TWO = """
meta:
  id: 记录任务-94
cases:
  - id: C-1
    type: 支出记录
    evidence: [custom_expense_log]
    turns:
      - role: user
        text: 记录午饭，15.3元
    rubric: 应记录
  - id: C-2
    type: 支出记录
    evidence: [custom_expense_log]
    turns:
      - role: user
        text: 记录晚饭，20元
    rubric: 应记录
"""


CASES_MIXED = """
meta:
  id: 记录任务-93
  multi_turn: true
cases:
  - id: A-1
    type: 锻炼记录
    input_mode: agent
    simulator: {goal: 让 agent 记录一次锻炼}
    evidence: [custom_exercise_log]
    rubric: 应记录
  - id: S-1
    type: 支出记录
    evidence: [custom_expense_log]
    turns:
      - role: user
        text: 记录午饭，15.3元
    rubric: 应记录
"""


class FakeAgent:
    """假被测评 agent：暴露 driver 需要的最小接口，并用 session/event 模拟一轮。"""
    def __init__(self, session_id: str = "fake-session", reply: str = "已记录", emit_events: bool = True):
        self._event_service = EventService()
        self._session = SimpleNamespace(meta_data=SimpleNamespace(session_id=session_id))
        self.reply = reply
        self.emit_events = emit_events
        self.sent: list[str] = []
        self.persisted = False
        self.cancelled = False

    async def send(self, text: str) -> None:
        self.sent.append(text)
        if not self.emit_events:
            return
        self._emit(
            "assistant/message",
            AssistantMessageData(message=Message(role="assistant", content=self.reply)),
        )
        self._emit("turn/end", TurnEndData(reason_type="success", reason_text=""))

    def _emit(self, type_: str, data) -> None:
        record = SessionRecordData(type=type_, seq=len(self.sent), data=data)
        self._event_service.trigger(SESSION_EVENT, SessionEventPayload(session_record=record))

    def persist_session_now(self) -> None:
        self.persisted = True

    def cancel(self) -> None:
        self.cancelled = True


def prepare(tmp_path: Path, cases_body: str = CASES_MIN, **runner_kwargs):
    """建 base + runner + cases，跑 _prepare_run，返回 (runner, ctx, case_set)。"""
    base = make_base(tmp_path)
    runner = make_runner(tmp_path, base, **runner_kwargs)
    cases_path = write_cases(tmp_path, cases_body)
    case_set = load_case_set(cases_path)
    ctx = runner._prepare_run(cases_path, case_set)
    return runner, ctx, case_set


# ---------------- _now ----------------


def test_now_是_utc_iso():
    dt = datetime.datetime.fromisoformat(EvalRunner._now())
    assert dt.tzinfo is not None
    assert dt.utcoffset() == datetime.timedelta(0)


# ---------------- _prepare_run ----------------


def test_prepare_run_建目录_复制工作副本_写runjson(tmp_path):
    runner, ctx, case_set = prepare(tmp_path)

    assert ctx.run_dir.is_dir()
    assert ctx.work_data_path.is_dir()
    assert ctx.session_folder == ctx.run_dir / "sessions"
    # 工作副本是 base 的拷贝，且 base 保持只读不被写
    assert (ctx.work_data_path / DB_REL).read_bytes() == b"DB-BASELINE"
    assert (ctx.work_data_path / "user" / "user.md").read_text(encoding="utf-8") == "只读内容"

    meta = json.loads((ctx.run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["run_id"] == ctx.run_id
    assert meta["cases_id"] == "记录任务-99"
    assert meta["dataset_version"] == 3
    assert meta["case_count"] == 1
    assert meta["started_at"]


def test_prepare_run_base_不存在时报错(tmp_path):
    runner = EvalRunner(base_dir=tmp_path / "nope", runs_dir=tmp_path / "runs")
    cases_path = write_cases(tmp_path, CASES_MIN)
    with pytest.raises(FileNotFoundError):
        runner._prepare_run(cases_path, load_case_set(cases_path))


# ---------------- _reset_state ----------------


def test_reset_state_把可变路径还原到基线(tmp_path):
    runner, ctx, case_set = prepare(tmp_path)
    case = case_set.cases[0]

    # 模拟上一个用例写坏了 work
    (ctx.work_data_path / DB_REL).write_bytes(b"CORRUPTED")
    (ctx.work_data_path / CP_REL).write_text("被污染", encoding="utf-8")

    runner._reset_state(ctx, case)

    assert (ctx.work_data_path / DB_REL).read_bytes() == b"DB-BASELINE"
    assert (ctx.work_data_path / CP_REL).read_text(encoding="utf-8") == BASE_CP_TEXT


def test_reset_state_不触碰只读文件(tmp_path):
    runner, ctx, case_set = prepare(tmp_path)
    runner._reset_state(ctx, case_set.cases[0])
    assert (ctx.work_data_path / "user" / "user.md").read_text(encoding="utf-8") == "只读内容"


# ---------------- _apply_precondition ----------------


def test_apply_precondition_无规则时不动文件(tmp_path):
    runner, ctx, case_set = prepare(tmp_path)
    runner._apply_precondition(ctx, case_set.cases[0])
    assert (ctx.work_data_path / CP_REL).read_text(encoding="utf-8") == BASE_CP_TEXT


def test_apply_precondition_写入规则章节(tmp_path):
    runner, ctx, case_set = prepare(tmp_path, CASES_WITH_RULES)
    runner._apply_precondition(ctx, case_set.cases[0])

    text = (ctx.work_data_path / CP_REL).read_text(encoding="utf-8")
    assert RULE_SECTION_TITLE in text
    assert "1. 锻炼->每日锻炼" in text
    assert "### 支出记录规则" in text


def test_apply_precondition_已有章节则替换不重复(tmp_path):
    runner, ctx, case_set = prepare(tmp_path, CASES_WITH_RULES)
    p = ctx.work_data_path / CP_REL
    p.write_text(
        "# 自定义记录规则\n\n## 关联记录规则\n9. 旧规则\n\n## 其它章节\n- 保留我\n",
        encoding="utf-8",
    )

    runner._apply_precondition(ctx, case_set.cases[0])

    text = p.read_text(encoding="utf-8")
    assert "1. 锻炼->每日锻炼" in text
    assert "9. 旧规则" not in text
    assert "## 其它章节" in text and "- 保留我" in text


def test_upsert_rule_section_空文本时新建():
    assert _upsert_rule_section("", ["A->B"]) == "## 关联记录规则\n1. A->B\n"


# ---------------- _snapshot_case ----------------


def test_snapshot_case_写出可解析的_cases_yaml(tmp_path):
    runner, _, case_set = prepare(tmp_path)
    case = case_set.cases[0]
    cdir = tmp_path / "cdir"

    runner._snapshot_case(case, cdir)

    data = yaml.safe_load((cdir / "case.yaml").read_text(encoding="utf-8"))
    assert data["id"] == "T-1"
    assert data["type"] == "支出记录"
    assert data["turns"][0]["text"] == "记录午饭，15.3元"
    assert data["evidence"] == ["custom_expense_log"]
    assert data["precondition"]["rules"] == []
    assert data["judge"]["mode"] == "model"


# ---------------- _write_summary ----------------


def test_write_summary_写本次run并追加全局(tmp_path):
    runner, ctx, _ = prepare(tmp_path)
    results = [
        CaseResult(
            case_id="T-1", case_type="支出记录", case_dir="d1", version="记录任务-99",
            content_summary="T-1·支出记录", session_id="s1", passed=True, reason="通过",
            started_at="2026-01-01T00:00:00+00:00", ended_at="2026-01-01T00:00:10+00:00",
        ),
        CaseResult(
            case_id="T-2", case_type="梦境记录", case_dir="d2", version="记录任务-99",
            content_summary="T-2·梦境记录", passed=None, multi_turn=True,
        ),
    ]

    runner._write_summary(ctx, results)

    run_csv = list(csv.DictReader((ctx.run_dir / "summary.csv").open(encoding="utf-8")))
    assert len(run_csv) == 2
    assert list(run_csv[0].keys()) == SUMMARY_COLUMNS
    assert run_csv[0]["是否通过"] == "Y"
    assert run_csv[0]["session_id"] == "s1"
    assert run_csv[0]["时间"] == "2026-01-01T00:00:10+00:00"
    assert run_csv[1]["是否通过"] == ""      # 未判
    assert run_csv[1]["多轮(Y/N)"] == "Y"

    global_csv = list(csv.DictReader((ctx.runs_dir / "summary.csv").open(encoding="utf-8")))
    assert len(global_csv) == 2

    # 再写一次：run 内覆盖、全局追加
    runner._write_summary(ctx, results)
    assert len(list(csv.DictReader((ctx.run_dir / "summary.csv").open(encoding="utf-8")))) == 2
    assert len(list(csv.DictReader((ctx.runs_dir / "summary.csv").open(encoding="utf-8")))) == 4


# ---------------- _run_under_test（e） ----------------


def test_run_under_test_单轮注入并收尾(tmp_path):
    fake = FakeAgent(session_id="sid-1")
    runner, ctx, case_set = prepare(tmp_path, agent_factory=lambda **kw: fake)

    sid = asyncio.run(runner._run_under_test(ctx, case_set.cases[0], tmp_path / "cdir"))

    assert sid == "sid-1"
    assert fake.sent == ["记录午饭，15.3元"]     # 按 turns 注入
    assert fake.persisted is True               # 先 flush
    assert fake.cancelled is True               # 再 cancel
    assert ctx.sessions["under_test"] is fake   # 留给步骤 g


def test_run_under_test_多轮按顺序注入(tmp_path):
    fake = FakeAgent()
    runner, ctx, case_set = prepare(tmp_path, CASES_TWO_TURNS, agent_factory=lambda **kw: fake)

    asyncio.run(runner._run_under_test(ctx, case_set.cases[0], tmp_path / "cdir"))

    assert fake.sent == ["记录午饭", "15.3元"]


def test_run_under_test_agent模式暂未实现(tmp_path):
    runner, ctx, case_set = prepare(tmp_path, CASES_AGENT_MODE, agent_factory=lambda **kw: FakeAgent())

    with pytest.raises(NotImplementedError, match="simulator"):
        asyncio.run(runner._run_under_test(ctx, case_set.cases[0], tmp_path / "cdir"))


def test_run_under_test_带trigger暂未实现(tmp_path):
    fake = FakeAgent()
    runner, ctx, case_set = prepare(tmp_path, CASES_TRIGGER, agent_factory=lambda **kw: fake)

    with pytest.raises(NotImplementedError, match="trigger"):
        asyncio.run(runner._run_under_test(ctx, case_set.cases[0], tmp_path / "cdir"))

    assert fake.sent == ["记录午饭"]   # 第一轮已发，第二轮遇 trigger 中断
    assert fake.cancelled is True       # finally 仍然收尾


def test_run_under_test_等不到turn_end时超时(tmp_path):
    fake = FakeAgent(emit_events=False)
    runner, ctx, case_set = prepare(
        tmp_path, agent_factory=lambda **kw: fake, turn_timeout=0.05
    )

    with pytest.raises(TimeoutError, match="超时"):
        asyncio.run(runner._run_under_test(ctx, case_set.cases[0], tmp_path / "cdir"))


# ---------------- run（端到端到 f） ----------------


def test_run_跑完到f并写summary(tmp_path):
    base = make_base(tmp_path)
    fake = FakeAgent(session_id="sid-run")
    runner = EvalRunner(base_dir=base, runs_dir=tmp_path / "runs", agent_factory=lambda **kw: fake)
    cases_path = write_cases(tmp_path, CASES_MIN)

    results = asyncio.run(runner.run(cases_path))

    assert len(results) == 1
    result = results[0]
    assert result.error == ""
    assert result.session_id == "sid-run"
    assert result.passed is None             # g~k 未实现 → 未判
    assert result.reason.startswith("TODO")
    assert result.started_at and result.ended_at
    assert result.started_at <= result.ended_at

    case_dir = Path(result.case_dir)
    assert case_dir.is_dir()
    assert case_dir.name == "000_T-1"
    assert case_dir.parent.name == "记录任务-99"
    assert (case_dir / "case.yaml").exists()
    assert fake.sent == ["记录午饭，15.3元"]

    rows = list(csv.DictReader((case_dir.parents[1] / "summary.csv").open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["session_id"] == "sid-run"
    assert rows[0]["是否通过"] == ""
    assert rows[0]["测试内容摘要"] == "T-1·支出记录"


def test_run_单条异常被兜住(tmp_path):
    def boom(**kwargs):
        raise RuntimeError("factory 挂了")

    base = make_base(tmp_path)
    runner = EvalRunner(base_dir=base, runs_dir=tmp_path / "runs", agent_factory=boom)
    cases_path = write_cases(tmp_path, CASES_MIN)

    results = asyncio.run(runner.run(cases_path))

    assert len(results) == 1
    assert results[0].error == "RuntimeError: factory 挂了"
    # 仍然落盘 summary
    assert len(list(csv.DictReader((runner.runs_dir / "summary.csv").open(encoding="utf-8")))) == 1


# ---------------- 补充边界 ----------------


def test_run_每条用例结束后清空ctx的用例状态(tmp_path):
    """用例级信息必须在 finally 里清掉：失败用例不能把残留对象留给下一条。"""
    base = make_base(tmp_path)
    fake = FakeAgent(session_id="sid")
    runner = EvalRunner(base_dir=base, runs_dir=tmp_path / "runs", agent_factory=lambda **kw: fake)

    captured: dict = {}
    original_prepare = runner._prepare_run

    def spy_prepare(cases_path, case_set):
        ctx = original_prepare(cases_path, case_set)
        captured["ctx"] = ctx
        return ctx

    runner._prepare_run = spy_prepare
    cases_path = write_cases(tmp_path, CASES_MIXED)

    results = asyncio.run(runner.run(cases_path))

    # 第 1 条在创建 agent 之前就失败（agent 模式未实现），第 2 条正常
    assert results[0].error.startswith("NotImplementedError")
    assert results[1].error == ""
    assert results[1].session_id == "sid"
    # 关键：ctx 上没有留下任何用例级信息
    assert captured["ctx"].sessions == {}


def test_run_under_test_agent模式失败时不残留上一条用例的agent(tmp_path):
    """直接验证残留隐患：失败用例结束前 ctx.sessions 为空。"""
    runner, ctx, _ = prepare(tmp_path, CASES_MIN, agent_factory=lambda **kw: FakeAgent())
    runner._agent_factory = lambda **kw: FakeAgent()
    case_set = load_case_set(write_cases(tmp_path, CASES_MIXED))

    # 先跑一条正常用例，把 agent 放进 ctx.sessions
    asyncio.run(runner._run_under_test(ctx, case_set.cases[1], tmp_path / "cdir"))
    assert "under_test" in ctx.sessions

    # 再跑一条 agent 模式的用例：它在赋值前就抛错，不应保留上一条的 agent
    runner._clear_case_state(ctx)   # run() 的 finally 会做这件事
    with pytest.raises(NotImplementedError):
        asyncio.run(runner._run_under_test(ctx, case_set.cases[0], tmp_path / "cdir2"))
    assert ctx.sessions == {}



class ObservingAgent(FakeAgent):
    """发送前先读取数据根里的 DB，再把它写脏——用于验证"用例之间会重置"。"""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.data_path: Path | None = None
        self.observed_db: list[bytes] = []

    async def send(self, text: str) -> None:
        db = self.data_path / DB_REL
        self.observed_db.append(db.read_bytes())
        db.write_bytes(f"POLLUTED:{text}".encode())
        await super().send(text)


def test_run_under_test_给工厂传的是工作副本与会话目录(tmp_path):
    seen: dict = {}

    def factory(**kwargs):
        seen.update(kwargs)
        return FakeAgent()

    runner, ctx, case_set = prepare(tmp_path, agent_factory=factory)
    asyncio.run(runner._run_under_test(ctx, case_set.cases[0], tmp_path / "cdir"))

    assert seen["data_path"] == ctx.work_data_path
    assert seen["session_folder"] == ctx.session_folder
    assert "name" not in seen   # 关键：不能传 name（否则 lifeprism 复刻 agent 的提示词查不到）


def test_run_用例之间会把可变状态重置(tmp_path):
    """共用底座的核心保证：后一个用例开始时，work 里的 DB 已还原为基线。"""
    base = make_base(tmp_path)
    agent = ObservingAgent()

    def factory(**kwargs):
        agent.data_path = kwargs["data_path"]
        return agent

    runner = EvalRunner(base_dir=base, runs_dir=tmp_path / "runs", agent_factory=factory)
    cases_path = write_cases(tmp_path, CASES_TWO)

    results = asyncio.run(runner.run(cases_path))

    assert [r.case_id for r in results] == ["C-1", "C-2"]
    assert [r.error for r in results] == ["", ""]
    assert [Path(r.case_dir).name for r in results] == ["000_C-1", "001_C-2"]
    # 第一个用例看到基线；第二个用例开始前已重置，所以也看到基线（而不是 C-1 写脏的值）
    assert agent.observed_db == [b"DB-BASELINE", b"DB-BASELINE"]
    assert agent.sent == ["记录午饭，15.3元", "记录晚饭，20元"]


def test_run_under_test_发送异常时仍然flush并cancel(tmp_path):
    class BoomAgent(FakeAgent):
        async def send(self, text: str) -> None:
            raise RuntimeError("send 挂了")

    agent = BoomAgent()
    runner, ctx, case_set = prepare(tmp_path, agent_factory=lambda **kw: agent)

    with pytest.raises(RuntimeError, match="send 挂了"):
        asyncio.run(runner._run_under_test(ctx, case_set.cases[0], tmp_path / "cdir"))

    assert agent.persisted is True
    assert agent.cancelled is True


def test_turn_collector_记录多step正文并忽略非字符串content():
    es = EventService()
    collector = TurnCollector(es)

    es.trigger(
        SESSION_EVENT,
        SessionEventPayload(
            session_record=SessionRecordData(
                type="assistant/message",
                seq=1,
                data=AssistantMessageData(
                    message=Message(role="assistant", content=[{"type": "text", "text": "分块内容"}])
                ),
            )
        ),
    )
    es.trigger(
        SESSION_EVENT,
        SessionEventPayload(
            session_record=SessionRecordData(
                type="assistant/message",
                seq=2,
                data=AssistantMessageData(message=Message(role="assistant", content="第一段")),
            )
        ),
    )

    assert collector.assistant_texts == ["第一段"]   # 非字符串 content 被忽略、不报错

    collector.expect_turn()
    es.trigger(
        SESSION_EVENT,
        SessionEventPayload(
            session_record=SessionRecordData(
                type="turn/end", seq=3, data=TurnEndData(reason_type="success", reason_text="")
            )
        ),
    )

    asyncio.run(collector.wait_turn(0.5))            # 已 set，立即返回
    assert collector.turn_ends == ["success"]
    assert collector.last_assistant_text == "第一段"
