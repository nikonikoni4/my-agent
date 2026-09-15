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
import sqlite3
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
    FAILURE_NOTE_MAX_CHARS,
    RULE_SECTION_TITLE,
    SUMMARY_COLUMNS,
    CaseResult,
    EvalRunner,
    TurnCollector,
    _failure_note,
    _read_turn_results,
    _under_test_failure,
    _upsert_rule_section,
    session_file_path,
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


@pytest.fixture
def short_tmp():
    """短路径临时目录。

    session 文件路径会把"数据根全路径"编码成一层目录名，Windows 上
    pytest 的 tmp_path 太长会撞到 MAX_PATH，故这些用例用短目录。
    """
    import shutil
    import tempfile
    import uuid

    path = Path(tempfile.gettempdir()) / f"ev-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


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
        self._emit("turn/end", TurnEndData(reason_type="success", reason_text="", error_type=""))

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


def test_run_跑完并写summary(tmp_path):
    base = make_base(tmp_path)
    fake = FakeAgent(session_id="sid-run")
    judge = FakeAgent(session_id="jid-run", reply='{"pass": true, "reason": "满足要点"}')
    runner = EvalRunner(
        base_dir=base,
        runs_dir=tmp_path / "runs",
        agent_factory=lambda **kw: fake,
        judge_factory=lambda **kw: judge,
    )
    cases_path = write_cases(tmp_path, CASES_MIN)

    results = asyncio.run(runner.run(cases_path))

    assert len(results) == 1
    result = results[0]
    assert result.error == ""
    assert result.session_id == "sid-run"
    assert result.passed is True
    assert result.reason == "满足要点"
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
    assert rows[0]["是否通过"] == "Y"
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
    runner = EvalRunner(
        base_dir=base,
        runs_dir=tmp_path / "runs",
        agent_factory=lambda **kw: fake,
        judge_factory=lambda **kw: FakeAgent(reply='{"pass": true, "reason": "ok"}'),
    )

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

    runner = EvalRunner(
        base_dir=base,
        runs_dir=tmp_path / "runs",
        agent_factory=factory,
        judge_factory=lambda **kw: FakeAgent(reply='{"pass": true, "reason": "ok"}'),
    )
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
                type="turn/end", seq=3, data=TurnEndData(reason_type="success", reason_text="", error_type="")
            )
        ),
    )

    asyncio.run(collector.wait_turn(0.5))            # 已 set，立即返回
    assert collector.turn_ends == ["success"]
    assert collector.last_assistant_text == "第一段"


# ---------------- g：session 复制改名 ----------------


class FileFakeAgent(FakeAgent):
    """在 send 时落一个真实 session 文件（模拟 SessionPresist 的行为）。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.data_path: Path | None = None
        self.session_folder: Path | None = None

    async def send(self, text: str) -> None:
        await super().send(text)
        sid = self._session.meta_data.session_id
        path = session_file_path(self.data_path, self.session_folder, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("session-line\n", encoding="utf-8")


def test_dump_sessions_按语义名复制到用例目录(short_tmp):
    agent = FileFakeAgent(session_id="sid-g")
    runner, ctx, case_set = prepare(short_tmp, agent_factory=lambda **kw: agent)
    agent.data_path = ctx.work_data_path
    agent.session_folder = ctx.session_folder

    asyncio.run(runner._run_under_test(ctx, case_set.cases[0], short_tmp / "cdir"))
    case_dir = short_tmp / "cdir"
    runner._dump_sessions(ctx, case_set.cases[0], case_dir)

    copied = case_dir / "session_under_test.jsonl"
    assert copied.exists()
    assert copied.read_text(encoding="utf-8") == "session-line\n"
    # 原文件仍在（复制而非移动）
    assert session_file_path(ctx.work_data_path, ctx.session_folder, "sid-g").exists()


def test_dump_sessions_文件不存在时跳过不报错(tmp_path):
    agent = FakeAgent(session_id="sid-missing")     # 不落文件
    runner, ctx, case_set = prepare(tmp_path, agent_factory=lambda **kw: agent)

    asyncio.run(runner._run_under_test(ctx, case_set.cases[0], tmp_path / "cdir"))
    case_dir = tmp_path / "cdir"
    runner._dump_sessions(ctx, case_set.cases[0], case_dir)

    assert not (case_dir / "session_under_test.jsonl").exists()


def test_dump_sessions_取不到session_id时跳过(tmp_path):
    runner, ctx, case_set = prepare(tmp_path, agent_factory=lambda **kw: FakeAgent())
    ctx.sessions["under_test"] = SimpleNamespace()   # 无 _session，取不到 session_id

    runner._dump_sessions(ctx, case_set.cases[0], tmp_path / "cdir")

    assert not (tmp_path / "cdir" / "session_under_test.jsonl").exists()


# ---------------- h：证据导出 ----------------


CASES_EVIDENCE = """
meta:
  id: 记录任务-92
cases:
  - id: E-1
    type: 支出记录
    evidence:
      - custom_expense_log
      - diary/<year>/<month>/<date>.md
    turns:
      - role: user
        text: 记录午饭，15.3元
    rubric: 应记录
"""

CASES_FILE_EVIDENCE = """
meta:
  id: 记录任务-91
cases:
  - id: F-1
    type: 规则变更
    evidence:
      - agent/chat/custom_prompt.md
    turns:
      - role: user
        text: 以后锻炼都不用打卡了
    rubric: 应改规则
"""


def init_expense_db(root: Path, created_ats: list[str]) -> None:
    """在给定数据根下造一个含 custom_expense_log 的真 sqlite 库。"""
    db = root / DB_REL
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()          # make_base 放的是占位字节，不是真库
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE custom_expense_log (id TEXT, amount REAL, content TEXT, "
        "expense_category TEXT, event_time TEXT, created_at TEXT, updated_at TEXT)"
    )
    for i, ts in enumerate(created_ats):
        con.execute(
            "INSERT INTO custom_expense_log VALUES (?,?,?,?,?,?,?)",
            (f"cre-{i}", 15.3, "午饭", "餐饮", ts, ts, ts),
        )
    con.commit()
    con.close()


def today_diary_rel() -> str:
    now = datetime.datetime.now()
    return f"diary/{now:%Y}/{now:%m}/{now:%Y-%m-%d}.md"


def test_export_evidence_表按时间窗取新增行(tmp_path):
    runner, ctx, case_set = prepare(tmp_path, CASES_EVIDENCE, agent_factory=lambda **kw: FakeAgent())
    init_expense_db(
        ctx.work_data_path,
        [
            "2026-01-01T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
            "2026-01-03T00:00:00+00:00",
        ],
    )
    case_dir = tmp_path / "cdir"

    evidence = runner._export_evidence(
        ctx, case_set.cases[0], "2026-01-02T00:00:00+00:00", "2026-01-02T23:59:59+00:00", case_dir
    )

    table = evidence["targets"]["custom_expense_log"]
    assert table["kind"] == "table"
    assert table["row_count"] == 1
    assert table["rows"][0]["created_at"] == "2026-01-02T00:00:00+00:00"
    assert "expense_category" in table["columns"]
    assert table["error"] == ""
    # 落盘
    saved = json.loads((case_dir / "evidence.json").read_text(encoding="utf-8"))
    assert saved["case_id"] == "E-1"
    assert saved["time_window"]["start"] == "2026-01-02T00:00:00+00:00"


def test_export_evidence_表不存在时给出错误(tmp_path):
    runner, ctx, case_set = prepare(tmp_path, CASES_EVIDENCE, agent_factory=lambda **kw: FakeAgent())
    init_expense_db(ctx.work_data_path, [])
    case_set.cases[0].evidence = ["no_such_table"]

    evidence = runner._export_evidence(
        ctx, case_set.cases[0], "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00",
        tmp_path / "cdir",
    )

    table = evidence["targets"]["no_such_table"]
    assert table["row_count"] == 0
    assert "表不存在" in table["error"]


def test_export_evidence_新增文件识别为new_file(tmp_path):
    runner, ctx, case_set = prepare(tmp_path, CASES_EVIDENCE, agent_factory=lambda **kw: FakeAgent())
    diary = ctx.work_data_path / today_diary_rel()
    diary.parent.mkdir(parents=True, exist_ok=True)
    diary.write_text("上午写代码\n下午看书\n", encoding="utf-8")

    evidence = runner._export_evidence(
        ctx, case_set.cases[0], "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00",
        tmp_path / "cdir",
    )

    item = evidence["targets"]["diary/<year>/<month>/<date>.md"]
    assert item["kind"] == "file"
    assert item["exists"] is True
    assert item["new_file"] is True
    assert item["changed"] is True
    assert item["added_lines"] == 2
    assert item["resolved_path"] == today_diary_rel()
    assert "+上午写代码" in item["diff"]


def test_export_evidence_未改动文件changed为false(tmp_path):
    runner, ctx, case_set = prepare(tmp_path, CASES_FILE_EVIDENCE, agent_factory=lambda **kw: FakeAgent())

    evidence = runner._export_evidence(
        ctx, case_set.cases[0], "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00",
        tmp_path / "cdir",
    )

    item = evidence["targets"]["agent/chat/custom_prompt.md"]
    assert item["changed"] is False
    assert item["diff"] == ""
    assert item["new_file"] is False


def test_export_evidence_改动文件给出git风格diff(tmp_path):
    runner, ctx, case_set = prepare(tmp_path, CASES_FILE_EVIDENCE, agent_factory=lambda **kw: FakeAgent())
    path = ctx.work_data_path / "agent" / "chat" / "custom_prompt.md"
    path.write_text(BASE_CP_TEXT + "## 关联记录规则\n1. 锻炼!->每日锻炼\n", encoding="utf-8")

    evidence = runner._export_evidence(
        ctx, case_set.cases[0], "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00",
        tmp_path / "cdir",
    )

    item = evidence["targets"]["agent/chat/custom_prompt.md"]
    assert item["changed"] is True
    assert item["added_lines"] >= 1
    assert item["diff"].startswith("--- base\n+++ current")
    assert "+1. 锻炼!->每日锻炼" in item["diff"]


def test_export_evidence_收集声明之外的改动文件(tmp_path):
    runner, ctx, case_set = prepare(tmp_path, CASES_EVIDENCE, agent_factory=lambda **kw: FakeAgent())
    # 1) 声明过的文件（日记）——不应重复出现在 other 里
    diary = ctx.work_data_path / today_diary_rel()
    diary.parent.mkdir(parents=True, exist_ok=True)
    diary.write_text("今天的事\n", encoding="utf-8")
    # 2) 未声明的文件被改——应被收进 other
    notes = ctx.work_data_path / "user" / "notes.md"
    notes.write_text("agent 顺手改了这里\n", encoding="utf-8")

    evidence = runner._export_evidence(
        ctx, case_set.cases[0], "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00",
        tmp_path / "cdir",
    )

    other = evidence["other_changed_files"]
    assert "user/notes.md" in other
    assert other["user/notes.md"]["new_file"] is True
    assert today_diary_rel() not in other


def test_export_evidence_可关闭全树扫描(tmp_path):
    runner, ctx, case_set = prepare(
        tmp_path, CASES_EVIDENCE, agent_factory=lambda **kw: FakeAgent(),
        scan_other_changed_files=False,
    )
    (ctx.work_data_path / "user" / "notes.md").write_text("改了\n", encoding="utf-8")

    evidence = runner._export_evidence(
        ctx, case_set.cases[0], "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00",
        tmp_path / "cdir",
    )

    assert evidence["other_changed_files"] == {}


def test_run_端到端到h产出四类文件(short_tmp):
    base = make_base(short_tmp)
    init_expense_db(base, ["2026-01-01T00:00:00+00:00"])
    agent = FileFakeAgent(session_id="sid-e2e")

    def factory(**kwargs):
        agent.data_path = kwargs["data_path"]
        agent.session_folder = kwargs["session_folder"]
        return agent

    runner = EvalRunner(
        base_dir=base,
        runs_dir=short_tmp / "runs",
        agent_factory=factory,
        judge_factory=lambda **kw: FakeAgent(reply='{"pass": true, "reason": "ok"}'),
    )
    cases_path = write_cases(short_tmp, CASES_EVIDENCE)

    results = asyncio.run(runner.run(cases_path))

    assert results[0].error == ""
    case_dir = Path(results[0].case_dir)
    assert (case_dir / "case.yaml").exists()
    assert (case_dir / "session_under_test.jsonl").exists()
    assert (case_dir / "evidence.json").exists()
    evidence = json.loads((case_dir / "evidence.json").read_text(encoding="utf-8"))
    assert set(evidence["targets"]) == {"custom_expense_log", "diary/<year>/<month>/<date>.md"}


# ---------------- i：裁判 ----------------


CASES_JUDGE_NONE = """
meta:
  id: 记录任务-90
cases:
  - id: J-0
    type: 支出记录
    judge: {mode: none}
    evidence: [custom_expense_log]
    turns:
      - role: user
        text: 记录午饭，15.3元
    rubric: 应记录
"""

SESSION_LINES = [
    {"type": "meta_data", "session_id": "s1", "name": "old_agent"},
    {"type": "turn/start", "data": {}},
    {
        "type": "user/message",
        "data": {"message": {"role": "user", "content": [{"type": "text", "text": "记录午饭，15.3元"}]}},
    },
    {"type": "assistant/message", "data": {"message": {"role": "assistant", "content": "已记录"}}},
    {
        "type": "tool/result",
        "data": {
            "tool_name": "create_custom_record_entry",
            "message": {"role": "tool", "content": "记录成功"},
        },
    },
]


def write_session(case_dir: Path, lines=SESSION_LINES) -> Path:
    case_dir.mkdir(parents=True, exist_ok=True)
    path = case_dir / "session_under_test.jsonl"
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "raw,expected_pass,has_error",
    [
        ('{"pass": true, "reason": "满足"}', True, False),
        ('{"pass": false, "reason": "时长不符"}', False, False),
        ('```json\n{"pass": true, "reason": "满足"}\n```', True, False),
        ('前置说明 {"pass": "true", "reason": "满足"} 后置', True, False),
        ("我觉得可以", None, True),
        ("{不是 JSON}", None, True),
        ('{"reason": "缺字段"}', None, True),
    ],
)
def test_parse_verdict_容错(raw, expected_pass, has_error):
    from lifeprismevalue.evalue.runner import _parse_verdict

    verdict = _parse_verdict(raw)
    assert verdict["pass"] is expected_pass
    assert bool(verdict["parse_error"]) is has_error


def test_read_transcript_只取三类消息并跳过异常行(tmp_path):
    from lifeprismevalue.evalue.runner import _read_transcript

    lines = [*SESSION_LINES, {"type": "step/end", "data": {}}]
    text = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n不是 JSON\n"
    path = tmp_path / "session.jsonl"
    path.write_text(text, encoding="utf-8")

    transcript = _read_transcript(path)
    assert "[user] 记录午饭，15.3元" in transcript
    assert "[assistant] 已记录" in transcript
    assert "[tool_result:create_custom_record_entry] 记录成功" in transcript
    assert "step/end" not in transcript
    # 文件不存在时返回空串，不抛错
    assert _read_transcript(tmp_path / "nope.jsonl") == ""


def test_run_judge_判通过并落盘judge_json(tmp_path):
    runner, ctx, case_set = prepare(
        tmp_path, agent_factory=lambda **kw: FakeAgent(),
        judge_factory=lambda **kw: FakeAgent(session_id="judge-1",
                                             reply='{"pass": true, "reason": "记录正确"}'),
    )
    case_dir = tmp_path / "cdir"
    write_session(case_dir)

    verdict = asyncio.run(
        runner._run_judge(ctx, case_set.cases[0], {"case_id": "T-1"}, case_dir)
    )

    assert verdict["pass"] is True
    assert verdict["reason"] == "记录正确"
    assert verdict["session_id"] == "judge-1"
    saved = json.loads((case_dir / "judge.json").read_text(encoding="utf-8"))
    assert saved["pass"] is True and saved["mode"] == "model"
    assert ctx.sessions["judge"] is not None


def test_run_judge_判不通过(tmp_path):
    runner, ctx, case_set = prepare(
        tmp_path, agent_factory=lambda **kw: FakeAgent(),
        judge_factory=lambda **kw: FakeAgent(reply='{"pass": false, "reason": "时长应为 30 分钟"}'),
    )
    case_dir = tmp_path / "cdir"
    write_session(case_dir)

    verdict = asyncio.run(runner._run_judge(ctx, case_set.cases[0], {}, case_dir))

    assert verdict["pass"] is False
    assert "30 分钟" in verdict["reason"]


def test_run_judge_无法解析时pass为None并保留原文(tmp_path):
    runner, ctx, case_set = prepare(
        tmp_path, agent_factory=lambda **kw: FakeAgent(),
        judge_factory=lambda **kw: FakeAgent(reply="我觉得不太行"),
    )
    case_dir = tmp_path / "cdir"
    write_session(case_dir)

    verdict = asyncio.run(runner._run_judge(ctx, case_set.cases[0], {}, case_dir))

    assert verdict["pass"] is None
    assert verdict["parse_error"]
    assert verdict["raw"] == "我觉得不太行"
    saved = json.loads((case_dir / "judge.json").read_text(encoding="utf-8"))
    assert saved["pass"] is None


def test_run_judge_输入包含rubric证据与对话(tmp_path):
    judge = FakeAgent(reply='{"pass": true, "reason": "ok"}')
    runner, ctx, case_set = prepare(
        tmp_path, agent_factory=lambda **kw: FakeAgent(), judge_factory=lambda **kw: judge
    )
    case_dir = tmp_path / "cdir"
    write_session(case_dir)

    asyncio.run(
        runner._run_judge(
            ctx, case_set.cases[0], {"targets": {"custom_expense_log": {"row_count": 1}}}, case_dir
        )
    )

    sent = judge.sent[0]
    assert "# 判分要点" in sent
    assert "应新增一条支出记录" in sent          # rubric 原文
    assert '"row_count": 1' in sent               # evidence 内容
    assert "# 对话记录" in sent
    assert "[user] 记录午饭，15.3元" in sent       # 来自 session
    assert "[tool_result:create_custom_record_entry] 记录成功" in sent


def test_run_judge_judge_mode_none时返回None且不落盘(tmp_path):
    runner, ctx, case_set = prepare(tmp_path, CASES_JUDGE_NONE, agent_factory=lambda **kw: FakeAgent())
    case_dir = tmp_path / "cdir"

    verdict = asyncio.run(runner._run_judge(ctx, case_set.cases[0], {}, case_dir))

    assert verdict is None
    assert not (case_dir / "judge.json").exists()


def test_run_端到端到i产出judge_json与结论(short_tmp):
    judge = FakeAgent(session_id="jid", reply='{"pass": true, "reason": "符合要点"}')
    runner = EvalRunner(
        base_dir=make_base(short_tmp),
        runs_dir=short_tmp / "runs",
        agent_factory=lambda **kw: FakeAgent(session_id="sid-i"),
        judge_factory=lambda **kw: judge,
    )
    cases_path = write_cases(short_tmp, CASES_MIN)

    results = asyncio.run(runner.run(cases_path))

    result = results[0]
    assert result.error == ""
    assert result.passed is True
    assert result.reason == "符合要点"
    case_dir = Path(result.case_dir)
    assert (case_dir / "judge.json").exists()

    rows = list(csv.DictReader((case_dir.parents[1] / "summary.csv").open(encoding="utf-8")))
    assert rows[0]["是否通过"] == "Y"
    assert rows[0]["测试结果摘要"] == "符合要点"
    assert (case_dir / "stats.json").exists()        # j 也会落盘（此用例 session 缺失，记 errors）


def test_run_裁判agent的session被落盘(short_tmp):
    judge = FileFakeAgent(session_id="judge-sid", reply='{"pass": true, "reason": "ok"}')

    def judge_factory(**kwargs):
        judge.data_path = kwargs["data_path"]
        judge.session_folder = kwargs["session_folder"]
        return judge

    runner = EvalRunner(
        base_dir=make_base(short_tmp),
        runs_dir=short_tmp / "runs",
        agent_factory=lambda **kw: FakeAgent(session_id="under-sid"),
        judge_factory=judge_factory,
    )
    results = asyncio.run(runner.run(write_cases(short_tmp, CASES_MIN)))

    case_dir = Path(results[0].case_dir)
    assert (case_dir / "session_judge.jsonl").exists()


# ---------------- j：统计 ----------------


STATS_SESSION_LINES = [
    {"type": "meta_data", "session_id": "s-stats", "name": "old_agent"},
    {"type": "turn/start", "turn": 1, "step": None, "data": {}},
    {"type": "step/start", "turn": 1, "step": 1, "data": {}},
    {
        "type": "user/message",
        "turn": 1,
        "step": 1,
        "data": {"message": {"role": "user", "content": "记录午饭"}},
    },
    {
        "type": "assistant/message",
        "turn": 1,
        "step": 1,
        "data": {
            "message": {"role": "assistant", "content": "已记录"},
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        },
    },
    {"type": "step/end", "turn": 1, "step": 1, "data": {"reason_type": "success", "reason_text": ""}},
    {"type": "turn/end", "turn": 1, "step": None, "data": {"reason_type": "success", "reason_text": ""}},
]


def write_stats_session(case_dir: Path, lines=STATS_SESSION_LINES) -> Path:
    case_dir.mkdir(parents=True, exist_ok=True)
    path = case_dir / "session_under_test.jsonl"
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


def test_run_stats_产出stats_json(tmp_path):
    runner, _, case_set = prepare(tmp_path, agent_factory=lambda **kw: FakeAgent())
    case_dir = tmp_path / "cdir"
    write_stats_session(case_dir)

    merged = runner._run_stats(case_set.cases[0], case_dir)

    assert set(merged["components"]) == {"TokenStats", "TimingStats", "PathStats"}
    assert merged["errors"] == {}
    assert merged["components"]["TokenStats"]["total"] == {
        "prompt": 100,
        "completion": 20,
        "total": 120,
    }
    saved = json.loads((case_dir / "stats.json").read_text(encoding="utf-8"))
    assert saved["components"]["TokenStats"]["step_count"] == 1


def test_run_stats_session缺失时记错误不抛出(tmp_path):
    runner, _, case_set = prepare(tmp_path, agent_factory=lambda **kw: FakeAgent())
    case_dir = tmp_path / "cdir"

    merged = runner._run_stats(case_set.cases[0], case_dir)

    assert merged["components"] == {}
    assert "session 文件不存在" in merged["errors"]["StatsRunner"]
    assert (case_dir / "stats.json").exists()


def test_run_stats_session内容非法时被容错跳过(tmp_path):
    """会话解析器逐行容错：坏行直接跳过，不抛错、不影响其它组件。"""
    runner, _, case_set = prepare(tmp_path, agent_factory=lambda **kw: FakeAgent())
    case_dir = tmp_path / "cdir"
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "session_under_test.jsonl").write_text("这不是 jsonl\n", encoding="utf-8")

    merged = runner._run_stats(case_set.cases[0], case_dir)

    assert merged["errors"] == {}
    assert merged["components"]["TokenStats"]["step_count"] == 0
    assert merged["components"]["PathStats"]["tool_call_count"] == 0
    assert (case_dir / "stats.json").exists()


# ---------------- 运行终态：turn 的最终结果 ----------------


def write_session_with_turns(case_dir: Path, turns: list[dict]) -> Path:
    """落一个只含若干条 turn/end 的 session 文件（模拟被测评 agent 的会话）。"""
    case_dir.mkdir(parents=True, exist_ok=True)
    path = case_dir / "session_under_test.jsonl"
    lines = [json.dumps({"type": "turn/end", **t}, ensure_ascii=False) for t in turns]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_read_turn_results_读出每轮终态(tmp_path):
    path = write_session_with_turns(
        tmp_path,
        [
            {"turn": 1, "data": {"reason_type": "success", "reason_text": ""}},
            {
                "turn": 2,
                "data": {
                    "reason_type": "error",
                    "reason_text": "AgentUnclaimedError: 无错误处理策略的错误",
                    "error_type": "AgentUnclaimedError",
                },
            },
        ],
    )

    results = _read_turn_results(path)

    assert [r["turn"] for r in results] == [1, 2]
    assert results[0]["reason_type"] == "success"
    assert results[1]["reason_type"] == "error"
    assert results[1]["error_type"] == "AgentUnclaimedError"
    assert "无错误处理策略" in results[1]["reason_text"]
    assert set(results[1]) == {"turn", "reason_type", "reason_text", "error_type"}


def test_read_turn_results_缺失字段与坏行都容错(tmp_path):
    """error_type / reason_text 缺失时读为空串；坏行/无关类型直接跳过。"""
    case_dir = tmp_path
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "session_under_test.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "step/end", "turn": 1, "data": {"reason_type": "success"}}),
                "不是 JSON",
                json.dumps({"type": "turn/end", "turn": 1, "data": {"reason_type": "error"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    results = _read_turn_results(case_dir / "session_under_test.jsonl")

    assert len(results) == 1
    assert results[0]["reason_text"] == ""
    assert results[0]["error_type"] == ""
    # 文件不存在时返回空列表，不抛错
    assert _read_turn_results(case_dir / "nope.jsonl") == []


@pytest.mark.parametrize(
    "turns,expected_turn",
    [
        ([], None),
        ([{"reason_type": "success", "reason_text": "", "error_type": "", "turn": 1}], None),
        (
            [
                {"reason_type": "success", "reason_text": "", "error_type": "", "turn": 1},
                {"reason_type": "error", "reason_text": "boom", "error_type": "", "turn": 2},
            ],
            2,
        ),
        (
            [
                {"reason_type": "error", "reason_text": "boom", "error_type": "", "turn": 1},
                {"reason_type": "success", "reason_text": "", "error_type": "", "turn": 2},
            ],
            1,
        ),
        ([{"reason_type": "interrupted", "reason_text": "取消", "error_type": "", "turn": 1}], 1),
    ],
)
def test_under_test_failure_取第一条非success(turns, expected_turn):
    failure = _under_test_failure(turns)
    assert (failure or {}).get("turn") == expected_turn


def test_failure_note_含类别与异常链并可截断():
    chain = "AgentUnclaimedError: 无错误处理策略的错误\n  MaxStepsExceededError: 达到最大步数20，强制终止本turn"
    note = _failure_note(
        {"turn": 2, "reason_type": "error", "error_type": "AgentUnclaimedError", "reason_text": chain}
    )
    assert note.startswith("运行未正常结束（turn 2 · error）")
    assert "AgentUnclaimedError" in note
    assert "MaxStepsExceededError" in note

    long_note = _failure_note(
        {"turn": 1, "reason_type": "error", "error_type": "", "reason_text": "x" * 5000}
    )
    assert long_note.endswith("（已截断）")
    assert len(long_note) <= FAILURE_NOTE_MAX_CHARS + len("运行未正常结束（turn 1 · error）：") + 10

    bare = _failure_note(
        {"turn": 1, "reason_type": "interrupted", "error_type": "", "reason_text": ""}
    )
    assert bare == "运行未正常结束（turn 1 · interrupted）"


class TurnEndAgent(FileFakeAgent):
    """模拟 SessionPresist：落的 session 文件带指定的 turn 终态。"""

    def __init__(
        self,
        reason_type: str = "success",
        reason_text: str = "",
        error_type: str = "",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.reason_type = reason_type
        self.reason_text = reason_text
        self.error_type = error_type

    async def send(self, text: str) -> None:
        """自己发事件（而非沿用父类恒为 success 的终态），使 collector 看到的终态与落盘一致。"""
        self.sent.append(text)
        self._emit(
            "assistant/message",
            AssistantMessageData(message=Message(role="assistant", content=self.reply)),
        )
        self._emit(
            "turn/end",
            TurnEndData(
                reason_type=self.reason_type, reason_text=self.reason_text, error_type=self.error_type
            ),
        )
        sid = self._session.meta_data.session_id
        path = session_file_path(self.data_path, self.session_folder, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "type": "turn/end",
                    "turn": 1,
                    "step": None,
                    "data": {
                        "reason_type": self.reason_type,
                        "reason_text": self.reason_text,
                        "error_type": self.error_type,
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )


def test_run_运行异常时不判定_记为未正常结束(short_tmp):
    """turn 终态是 error：不跑裁判、不重试，证据与统计仍落盘，summary 反映未正常结束。"""
    agent = TurnEndAgent(
        session_id="sid-fail",
        reason_type="error",
        error_type="AgentUnclaimedError",
        reason_text="AgentUnclaimedError: 无错误处理策略的错误\n  LLMConnectionError: peer closed connection",
    )

    def agent_factory(**kwargs):
        agent.data_path = kwargs["data_path"]
        agent.session_folder = kwargs["session_folder"]
        return agent

    judge_calls: list[int] = []

    def judge_factory(**kwargs):
        judge_calls.append(1)
        return FakeAgent(reply='{"pass": true, "reason": "不应被调用"}')

    runner = EvalRunner(
        base_dir=make_base(short_tmp),
        runs_dir=short_tmp / "runs",
        agent_factory=agent_factory,
        judge_factory=judge_factory,
    )

    results = asyncio.run(runner.run(write_cases(short_tmp, CASES_MIN)))

    result = results[0]
    assert judge_calls == []                       # 未判定
    assert result.passed is None
    assert "运行未正常结束" in result.error
    assert "peer closed connection" in result.reason

    case_dir = Path(result.case_dir)
    assert not (case_dir / "judge.json").exists()
    assert (case_dir / "evidence.json").exists()   # 证据仍落盘，供排查
    assert (case_dir / "stats.json").exists()      # 统计仍落盘

    rows = list(csv.DictReader((case_dir.parents[1] / "summary.csv").open(encoding="utf-8")))
    assert rows[0]["是否通过"] == ""                # 未判定 → 不是 Y 也不是 N
    assert "运行未正常结束" in rows[0]["测试结果摘要"]


def test_run_运行正常时仍照常判定(short_tmp):
    """对照组：turn 终态为 success 时，走原来的判定路径。"""
    agent = TurnEndAgent(session_id="sid-ok", reason_type="success")

    def agent_factory(**kwargs):
        agent.data_path = kwargs["data_path"]
        agent.session_folder = kwargs["session_folder"]
        return agent

    runner = EvalRunner(
        base_dir=make_base(short_tmp),
        runs_dir=short_tmp / "runs",
        agent_factory=agent_factory,
        judge_factory=lambda **kw: FakeAgent(reply='{"pass": true, "reason": "符合要点"}'),
    )

    results = asyncio.run(runner.run(write_cases(short_tmp, CASES_MIN)))

    assert results[0].error == ""
    assert results[0].passed is True
    assert (Path(results[0].case_dir) / "judge.json").exists()


def test_drive_scripted_某轮失败后停止后续轮次注入(short_tmp):
    """首轮以 error 收场：不再注入第二轮（否则 send 会重启已终止的 loop）。"""
    agent = TurnEndAgent(session_id="sid-stop", reason_type="error", reason_text="boom")

    def agent_factory(**kwargs):
        agent.data_path = kwargs["data_path"]
        agent.session_folder = kwargs["session_folder"]
        return agent

    runner = EvalRunner(
        base_dir=make_base(short_tmp),
        runs_dir=short_tmp / "runs",
        agent_factory=agent_factory,
        judge_factory=lambda **kw: FakeAgent(reply='{"pass": true, "reason": "不应被调用"}'),
    )

    results = asyncio.run(runner.run(write_cases(short_tmp, CASES_TWO_TURNS)))

    assert agent.sent == ["记录午饭"]            # 第二轮未注入
    assert results[0].passed is None
    assert "运行未正常结束" in results[0].reason

