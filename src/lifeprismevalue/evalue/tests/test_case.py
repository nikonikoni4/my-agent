"""单条用例执行实体（CaseExecutor）的测试：b~j 全流程 + 运行终态 + 用例级兜底。

被测 agent / 裁判都是假实现（fakes.py），不碰 LLM；用例级测试跑在**同进程**里
（不经过子进程），跨进程那条通道由 test_worker.py 与 test_runner.py 覆盖。
"""

from __future__ import annotations

import asyncio
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

from lifeprismevalue.evalue.case import (
    MODEL_NOT_USED,
    MODEL_UNKNOWN,
    RULE_SECTION_TITLE,
    CaseExecutor,
    TurnCollector,
    collect_agent_info,
    failure_note,
    _parse_score,
    _parse_verdict,
    _read_transcript,
    read_turn_results,
    under_test_failure,
    _upsert_rule_section,
    session_file_path,
)
from fakes import (
    SESSION_LINES,
    STATS_SESSION_LINES,
    FakeAgent,
    FileFakeAgent,
    TurnEndAgent,
    WriteFakeAgent,
    write_session,
    write_turn_session,
)
from helpers import (
    BASE_CP_TEXT,
    CASES_AGENT_MODE,
    CASES_EVIDENCE,
    CASES_JUDGE_NONE,
    CASES_MIN,
    CASES_TRIGGER,
    CASES_TWO_TURNS,
    CASES_WITH_RULES,
    CP_REL,
    DB_REL,
    init_expense_db,
    load_case,
    make_base,
    make_context,
    today_diary_rel,
)

PASS_REPLY = '{"pass": true, "reason": "ok"}'


# ---------------- 夹具 ----------------


def agent_factory_for(agent):
    """把假 agent 包成工厂，并把 data_path / session_folder 记到它身上。"""

    def factory(**kwargs):
        agent.data_path = kwargs.get("data_path")
        agent.session_folder = kwargs.get("session_folder")
        agent.factory_kwargs = kwargs
        return agent

    return factory


def run_case(tmp_path: Path, body: str = CASES_MIN, *, agent=None, judge=None, **ctx_kw):
    """跑一条用例，返回 (ctx, result)。"""
    ctx, _ = make_context(tmp_path, body, **ctx_kw)
    agent = agent if agent is not None else FakeAgent()
    judge = judge if judge is not None else FakeAgent(session_id="judge-sid", reply=PASS_REPLY)
    executor = CaseExecutor(
        agent_factory=agent_factory_for(agent),
        judge_factory=agent_factory_for(judge),
    )
    return ctx, executor.run(ctx)


# ---------------- b：precondition ----------------


def test_precondition_无规则时不动文件(tmp_path) -> None:
    ctx, _ = make_context(tmp_path, CASES_MIN)
    CaseExecutor()._apply_precondition(ctx)

    assert (ctx.env_root / CP_REL).read_text(encoding="utf-8") == BASE_CP_TEXT


def test_precondition_写入规则章节(tmp_path) -> None:
    ctx, _ = make_context(tmp_path, CASES_WITH_RULES)
    CaseExecutor()._apply_precondition(ctx)

    text = (ctx.env_root / CP_REL).read_text(encoding="utf-8")
    assert RULE_SECTION_TITLE in text
    assert "1. 锻炼->每日锻炼" in text
    assert "### 支出记录规则" in text


def test_precondition_已有章节则替换不重复(tmp_path) -> None:
    ctx, _ = make_context(tmp_path, CASES_WITH_RULES)
    path = ctx.env_root / CP_REL
    path.write_text(
        "# 自定义记录规则\n\n## 关联记录规则\n9. 旧规则\n\n## 其它章节\n- 保留我\n",
        encoding="utf-8",
    )

    CaseExecutor()._apply_precondition(ctx)

    text = path.read_text(encoding="utf-8")
    assert "1. 锻炼->每日锻炼" in text
    assert "9. 旧规则" not in text
    assert "## 其它章节" in text and "- 保留我" in text


def test_upsert_rule_section_空文本时新建() -> None:
    assert _upsert_rule_section("", ["A->B"]) == "## 关联记录规则\n1. A->B\n"


# ---------------- c：用例快照 ----------------


def test_snapshot_case_写出可解析的_case_yaml(tmp_path) -> None:
    ctx, _ = make_context(tmp_path, CASES_MIN)
    CaseExecutor()._snapshot_case(ctx)

    data = yaml.safe_load((ctx.case_dir / "case.yaml").read_text(encoding="utf-8"))
    assert data["id"] == "T-1"
    assert data["type"] == "支出记录"
    assert data["turns"][0]["text"] == "记录午饭，15.3元"
    assert data["evidence"] == ["custom_expense_log"]
    assert data["precondition"]["rules"] == []
    assert data["judge"]["mode"] == "model"


# ---------------- e：跑 under_test ----------------


def test_单轮注入并按序收尾(tmp_path) -> None:
    ctx, result = run_case(tmp_path)
    agent = ctx.sessions["under_test"]

    assert result["session_id"] == "fake-session"
    assert agent.sent == ["记录午饭，15.3元"]     # 按 turns 注入
    assert agent.persisted is True               # 先 flush
    assert agent.cancelled is True               # 再 cancel


def test_多轮按顺序注入(tmp_path) -> None:
    ctx, _ = run_case(tmp_path, CASES_TWO_TURNS)
    assert ctx.sessions["under_test"].sent == ["记录午饭", "15.3元"]


def test_给工厂传的是环境数据根与会话目录(tmp_path) -> None:
    ctx, _ = run_case(tmp_path)
    seen = ctx.sessions["under_test"].factory_kwargs

    assert seen["data_path"] == ctx.env_root
    assert seen["session_folder"] == ctx.session_folder
    # 关键：不能传 name（否则 lifeprism 复刻 agent 的提示词查不到）
    assert "name" not in seen


def test_agent模式未实现时收进结果(tmp_path) -> None:
    _, result = run_case(tmp_path, CASES_AGENT_MODE)

    assert result["passed"] is None
    assert result["error"].startswith("NotImplementedError")
    assert "simulator" in result["error"]


def test_带trigger未实现时收进结果(tmp_path) -> None:
    agent = FakeAgent()
    ctx, result = run_case(tmp_path, CASES_TRIGGER, agent=agent)

    assert "trigger" in result["error"]
    assert agent.sent == ["记录午饭"]        # 第一轮已发，第二轮遇 trigger 中断
    assert agent.cancelled is True           # finally 仍然收尾


def test_等不到turn_end时超时收口(tmp_path) -> None:
    agent = FakeAgent(emit_events=False)
    _, result = run_case(tmp_path, agent=agent, turn_timeout=0.05)

    assert "超时" in result["error"]
    assert agent.persisted is True and agent.cancelled is True


def test_发送异常时仍然flush并cancel(tmp_path) -> None:
    class BoomAgent(FakeAgent):
        async def send(self, text: str) -> None:
            raise RuntimeError("send 挂了")

    agent = BoomAgent()
    _, result = run_case(tmp_path, agent=agent)

    assert "send 挂了" in result["error"]
    assert agent.persisted is True and agent.cancelled is True


def test_结果字段齐全(tmp_path) -> None:
    _, result = run_case(tmp_path)

    assert set(result) == {
        "case_id", "case_type", "case_dir", "version", "content_summary", "session_id",
        "passed", "score", "reason", "multi_turn", "started_at", "ended_at", "error",
        "agents", "models",
    }
    assert result["case_id"] == "T-1"
    assert result["version"] == "记录任务-99"
    assert result["content_summary"] == "T-1·支出记录"
    assert result["started_at"] and result["started_at"] <= result["ended_at"]
    # PASS_REPLY 里没有 score：判定有效，只是这次没拿到分
    assert result["score"] is None


# ---------------- g：session 复制改名 ----------------


def test_dump_sessions_按语义名复制到用例目录(short_tmp) -> None:
    agent = FileFakeAgent(session_id="sid-g")
    ctx, _ = run_case(short_tmp, agent=agent)

    copied = ctx.case_dir / "session_under_test.jsonl"
    assert copied.read_text(encoding="utf-8") == "session-line\n"
    # 原文件仍在（复制而非移动）
    assert session_file_path(ctx.env_root, ctx.session_folder, "sid-g").exists()


def test_dump_sessions_文件不存在时跳过不报错(tmp_path) -> None:
    ctx, _ = run_case(tmp_path)          # FakeAgent 不落文件
    assert not (ctx.case_dir / "session_under_test.jsonl").exists()


def test_dump_sessions_取不到session_id时跳过(tmp_path) -> None:
    ctx, _ = make_context(tmp_path, CASES_MIN)
    ctx.sessions["under_test"] = SimpleNamespace()   # 无 _session
    CaseExecutor()._dump_sessions(ctx)

    assert not (ctx.case_dir / "session_under_test.jsonl").exists()


def test_裁判的session也会被落盘(short_tmp) -> None:
    judge = FileFakeAgent(session_id="judge-g", reply=PASS_REPLY)
    ctx, _ = run_case(short_tmp, judge=judge)

    assert (ctx.case_dir / "session_judge.jsonl").exists()


# ---------------- h：证据 ----------------


def test_导出evidence_json(tmp_path) -> None:
    """证据 = 与环境基线（a 步快照）对比出来的差异（表按 id 比出新增行）。

    写入必须发生在跑的过程中（假 agent 在 send 时落库）：基线打在跑之前，
    跑之前就把环境改好等于让快照把改动一起吞掉。
    """
    base = make_base(tmp_path)
    init_expense_db(base, [{"id": "old"}])          # 底座里已有历史基线

    def write(data_path: Path) -> None:
        init_expense_db(data_path, [{"id": "old"}, {"id": "new"}])

    ctx, _ = run_case(tmp_path, CASES_EVIDENCE, base_dir=base, agent=WriteFakeAgent(write))

    evidence = json.loads((ctx.case_dir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["case_id"] == "E-1"
    assert set(evidence["targets"]) == {"custom_expense_log", "diary/<year>/<month>/<date>.md"}
    table = evidence["targets"]["custom_expense_log"]
    assert [row["id"] for row in table["rows"]] == ["new"]
    assert evidence["targets"]["diary/<year>/<month>/<date>.md"]["resolved_path"] == today_diary_rel()


def test_证据_日记新增文件被标成_new_file(tmp_path) -> None:
    base = make_base(tmp_path)

    def write(data_path: Path) -> None:
        diary = data_path / today_diary_rel()
        diary.parent.mkdir(parents=True, exist_ok=True)
        diary.write_text("上午写代码\n", encoding="utf-8")

    ctx, _ = run_case(tmp_path, CASES_EVIDENCE, base_dir=base, agent=WriteFakeAgent(write))

    evidence = json.loads((ctx.case_dir / "evidence.json").read_text(encoding="utf-8"))
    item = evidence["targets"]["diary/<year>/<month>/<date>.md"]
    assert item["new_file"] is True and item["changed"] is True


def test_基线快照打在_precondition_之前(tmp_path) -> None:
    """precondition 写进 custom_prompt.md 的规则本身也是证据。

    有用例把规则文件当证据（判「规则文件终态」）。快照若打在 precondition 之后，
    这一步就被藏起来了：规则写没写对，`evidence` 里看不见。
    """
    ctx, _ = run_case(tmp_path, CASES_WITH_RULES)

    evidence = json.loads((ctx.case_dir / "evidence.json").read_text(encoding="utf-8"))
    item = evidence["other_changed_files"][CP_REL]
    assert item["changed"] is True
    assert "+1. 锻炼->每日锻炼" in item["diff"]


def test_写到未声明的表也会出现在证据里(tmp_path) -> None:
    """h 步的接线：agent 把内容写到**别的表**，全库扫描要能把它收进证据。

    这是"写错地方"唯一能被看见的通道（表类证据只按声明的表取）。
    """
    base = make_base(tmp_path)

    def write(data_path: Path) -> None:
        con = sqlite3.connect(data_path / DB_REL)
        with con:
            con.execute("INSERT INTO custom_exercise_log VALUES ('e1', 20, '平板支撑和臀桥')")
        con.close()

    ctx, _ = run_case(tmp_path, CASES_MIN, base_dir=base, agent=WriteFakeAgent(write))

    evidence = json.loads((ctx.case_dir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["targets"]["custom_expense_log"]["row_count"] == 0
    assert "custom_exercise_log" in evidence["other_changed_tables"]


# ---------------- 失败现场：跑完后的环境 ----------------
# core 的 keep_env_on_failure 只看「执行通道失败」（子进程起不来/崩了/没产出结果）；
# 「运行未正常结束」与「判定不通过」在 core 眼里是成功，环境会被 reset/dispose 掉。
# 这两类现场只能由领域层在返回前自己留。


def test_判定不通过时留下跑完后的环境(tmp_path) -> None:
    ctx, result = run_case(
        tmp_path, CASES_WITH_RULES, judge=FakeAgent(reply='{"pass": false, "reason": "规则没写对"}')
    )

    assert result["passed"] is False
    kept = ctx.case_dir / "env"
    assert kept.is_dir(), "判定不通过时必须留下现场"
    assert (kept / CP_REL).is_file(), "留下的应该是整份数据根"


def test_运行未正常结束时留下跑完后的环境(short_tmp) -> None:
    agent = TurnEndAgent(session_id="sid-err", reason_type="error", reason_text="boom")

    ctx, result = run_case(short_tmp, agent=agent)

    assert result["passed"] is None and "运行未正常结束" in result["error"]
    assert (ctx.case_dir / "env").is_dir()


def test_留下的副本是跑完之后的状态(tmp_path) -> None:
    """与 `baseline/` 成对：baseline 是跑前，env/ 是跑后。

    判据取 precondition 写进去的规则——它只可能出现在"跑之后"那一份里。
    """
    ctx, _ = run_case(
        tmp_path, CASES_WITH_RULES, judge=FakeAgent(reply='{"pass": false, "reason": "x"}')
    )

    kept = (ctx.case_dir / "env" / CP_REL).read_text(encoding="utf-8")
    before = (ctx.case_dir / "baseline" / CP_REL).read_text(encoding="utf-8")
    assert "1. 锻炼->每日锻炼" in kept, "跑之后的副本应该含 precondition 写入"
    assert "1. 锻炼->每日锻炼" not in before, "跑之前的快照不该含 precondition 写入"


def test_通过时不留副本(tmp_path) -> None:
    """没出事就不占磁盘：通过时用例目录里只有 baseline/。"""
    ctx, result = run_case(tmp_path)

    assert result["passed"] is True
    assert (ctx.case_dir / "baseline").is_dir()
    assert not (ctx.case_dir / "env").exists()


def test_未判定时不误留副本(tmp_path) -> None:
    """judge.mode=none 时 passed 是 None（不是"不通过"），不该留现场。"""
    ctx, result = run_case(tmp_path, CASES_JUDGE_NONE)

    assert result["passed"] is None
    assert not (ctx.case_dir / "env").exists()


# ---------------- i：裁判 ----------------


def test_裁判_判通过并落盘(tmp_path) -> None:
    _, result = run_case(tmp_path, judge=FakeAgent(reply='{"pass": true, "reason": "记录正确"}'))

    assert result["passed"] is True
    assert result["reason"] == "记录正确"
    assert (Path(result["case_dir"]) / "judge.json").exists()


def test_裁判_判不通过(tmp_path) -> None:
    _, result = run_case(tmp_path, judge=FakeAgent(reply='{"pass": false, "reason": "时长应为 30 分钟"}'))

    assert result["passed"] is False
    assert "30 分钟" in result["reason"]


def test_裁判_得分写进结果与judge_json(tmp_path) -> None:
    """rubric 自带评分要求时：分数由裁判按它折算，可以与 pass 各判各的。"""
    ctx, result = run_case(
        tmp_path, judge=FakeAgent(reply='{"pass": true, "score": 60, "reason": "60 分及格"}')
    )

    assert result["score"] == 60 and result["passed"] is True
    saved = json.loads((ctx.case_dir / "judge.json").read_text(encoding="utf-8"))
    assert saved["score"] == 60


def test_裁判_没给分时结果留空但判定仍然有效(tmp_path) -> None:
    """score 缺失不该把判定一起拖下水：pass 有值就算判定成立。"""
    _, result = run_case(tmp_path, judge=FakeAgent(reply='{"pass": false, "reason": "少记一条"}'))

    assert result["passed"] is False
    assert result["score"] is None


def test_裁判_无法解析时pass为None并保留原文(tmp_path) -> None:
    ctx, result = run_case(tmp_path, judge=FakeAgent(reply="我觉得不太行"))

    assert result["passed"] is None
    saved = json.loads((ctx.case_dir / "judge.json").read_text(encoding="utf-8"))
    assert saved["pass"] is None
    assert saved["parse_error"]
    assert saved["raw"] == "我觉得不太行"


def test_裁判_输入包含判分要点证据与对话(tmp_path) -> None:
    judge = FakeAgent(reply=PASS_REPLY)
    ctx, _ = make_context(tmp_path, CASES_MIN)
    write_session(ctx.case_dir)          # 模拟 g 步复制出来的 session（含三类消息）
    executor = CaseExecutor(judge_factory=agent_factory_for(judge))

    verdict = asyncio.run(
        executor._run_judge(ctx, {"targets": {"custom_expense_log": {"row_count": 1}}})
    )

    sent = judge.sent[0]
    assert "# 判分要点" in sent
    assert "应新增一条支出记录" in sent          # rubric 原文
    assert '"row_count": 1' in sent               # evidence 内容
    assert "# 对话记录" in sent
    assert "[user] 记录午饭，15.3元" in sent       # 来自 session
    assert "[tool_result:create_custom_record_entry] 记录成功" in sent
    assert verdict["pass"] is True


def test_裁判_mode_none时未判定且不落盘(tmp_path) -> None:
    _, result = run_case(tmp_path, CASES_JUDGE_NONE)

    assert result["passed"] is None
    assert result["score"] is None          # 没判就没分，不留 0（0 是「判了且不达标」）
    assert "judge.mode=none" in result["reason"]
    assert not (Path(result["case_dir"]) / "judge.json").exists()


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
def test_parse_verdict_容错(raw, expected_pass, has_error) -> None:
    verdict = _parse_verdict(raw)
    assert verdict["pass"] is expected_pass
    assert bool(verdict["parse_error"]) is has_error


def test_parse_verdict_取到得分() -> None:
    verdict = _parse_verdict('```json\n{"pass": true, "score": 80, "reason": "大体符合"}\n```')

    assert verdict["pass"] is True and verdict["score"] == 80 and verdict["parse_error"] == ""


@pytest.mark.parametrize(
    "value,expected",
    [
        (85, 85),
        (0, 0),
        (100, 100),
        (85.6, 86),          # 浮点四舍五入
        ("85", 85),          # 字符串数字
        (" 85 ", 85),
        (120, 100),          # 越界夹回 [0, 100]
        (-10, 0),
        (None, None),
        ("满分", None),       # 不是数字
        (True, None),        # bool 是 int 的子类，必须挡掉（否则 true 会变成 1 分）
        (float("nan"), None),  # json.loads 默认接受 NaN 字面量
        (float("inf"), None),
    ],
)
def test_parse_score_容错(value, expected) -> None:
    assert _parse_score(value) == expected


def test_parse_verdict_得分非法不写parse_error() -> None:
    """得分取不到只让它自己为 None：parse_error 的语义是「整条判定不成立」。"""
    verdict = _parse_verdict('{"pass": true, "score": "满分", "reason": "ok"}')

    assert verdict["pass"] is True
    assert verdict["score"] is None
    assert verdict["parse_error"] == ""


def test_read_transcript_只取三类消息并跳过异常行(tmp_path) -> None:
    lines = [*SESSION_LINES, {"type": "step/end", "data": {}}]
    path = tmp_path / "session.jsonl"
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n不是 JSON\n",
        encoding="utf-8",
    )

    transcript = _read_transcript(path)

    assert "[user] 记录午饭，15.3元" in transcript
    assert "[assistant] 已记录" in transcript
    assert "[tool_result:create_custom_record_entry] 记录成功" in transcript
    assert "step/end" not in transcript
    assert _read_transcript(tmp_path / "nope.jsonl") == ""   # 文件不存在不抛错


# ---------------- j：统计 ----------------


def test_stats_产出_stats_json(tmp_path) -> None:
    ctx, _ = make_context(tmp_path, CASES_MIN)
    write_session(ctx.case_dir, STATS_SESSION_LINES)

    merged = CaseExecutor()._run_stats(ctx)

    assert set(merged["components"]) == {"TokenStats", "TimingStats", "PathStats"}
    assert merged["errors"] == {}
    assert merged["components"]["TokenStats"]["total"] == {
        "prompt": 100,
        "completion": 20,
        "total": 120,
    }
    saved = json.loads((ctx.case_dir / "stats.json").read_text(encoding="utf-8"))
    assert saved["components"]["TokenStats"]["step_count"] == 1


def test_stats_session缺失时记错误不抛出(tmp_path) -> None:
    ctx, _ = make_context(tmp_path, CASES_MIN)

    merged = CaseExecutor()._run_stats(ctx)

    assert merged["components"] == {}
    assert "session 文件不存在" in merged["errors"]["StatsRunner"]
    assert (ctx.case_dir / "stats.json").exists()


def test_stats_session内容非法时被容错跳过(tmp_path) -> None:
    ctx, _ = make_context(tmp_path, CASES_MIN)
    ctx.case_dir.mkdir(parents=True, exist_ok=True)
    (ctx.case_dir / "session_under_test.jsonl").write_text("这不是 jsonl\n", encoding="utf-8")

    merged = CaseExecutor()._run_stats(ctx)

    assert merged["errors"] == {}
    assert merged["components"]["TokenStats"]["step_count"] == 0
    assert merged["components"]["PathStats"]["tool_call_count"] == 0


# ---------------- 运行终态 ----------------


def test_read_turn_results_读出每轮终态(tmp_path) -> None:
    path = write_turn_session(
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

    results = read_turn_results(path)

    assert [r["turn"] for r in results] == [1, 2]
    assert results[0]["reason_type"] == "success"
    assert results[1]["error_type"] == "AgentUnclaimedError"
    assert "无错误处理策略" in results[1]["reason_text"]
    assert set(results[1]) == {"turn", "reason_type", "reason_text", "error_type"}


def test_read_turn_results_缺失字段与坏行都容错(tmp_path) -> None:
    path = tmp_path / "session_under_test.jsonl"
    path.write_text(
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

    results = read_turn_results(path)

    assert len(results) == 1
    assert results[0]["reason_text"] == ""
    assert results[0]["error_type"] == ""
    assert read_turn_results(tmp_path / "nope.jsonl") == []


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
def test_under_test_failure_取第一条非success(turns, expected_turn) -> None:
    assert (under_test_failure(turns) or {}).get("turn") == expected_turn


def test_failure_note_含类别与异常链并可截断() -> None:
    chain = "AgentUnclaimedError: 无错误处理策略的错误\n  MaxStepsExceededError: 达到最大步数20"
    note = failure_note(
        {"turn": 2, "reason_type": "error", "error_type": "AgentUnclaimedError", "reason_text": chain}
    )
    assert note.startswith("运行未正常结束（turn 2 · error）")
    assert "AgentUnclaimedError" in note and "MaxStepsExceededError" in note

    bare = failure_note(
        {"turn": 1, "reason_type": "interrupted", "error_type": "", "reason_text": ""}
    )
    assert bare == "运行未正常结束（turn 1 · interrupted）"


def test_运行异常时不判定_证据与统计仍落盘(short_tmp) -> None:
    """turn 终态是 error：不跑裁判、不重试，证据与统计仍落盘。"""
    agent = TurnEndAgent(
        session_id="sid-fail",
        reason_type="error",
        error_type="AgentUnclaimedError",
        reason_text="AgentUnclaimedError: 无错误处理策略的错误\n  LLMConnectionError: peer closed connection",
    )
    judge = FakeAgent(reply=PASS_REPLY)

    ctx, result = run_case(short_tmp, agent=agent, judge=judge)

    assert result["passed"] is None
    assert result["score"] is None                           # 没判就没分
    assert "运行未正常结束" in result["error"]
    assert "peer closed connection" in result["reason"]
    assert judge.sent == []                                  # 未判
    assert not (ctx.case_dir / "judge.json").exists()
    assert (ctx.case_dir / "evidence.json").exists()          # 证据仍落盘，供排查
    assert (ctx.case_dir / "stats.json").exists()


def test_运行正常时仍照常判定(short_tmp) -> None:
    agent = TurnEndAgent(session_id="sid-ok", reason_type="success")
    ctx, result = run_case(short_tmp, agent=agent, judge=FakeAgent(reply='{"pass": true, "reason": "符合要点"}'))

    assert result["error"] == ""
    assert result["passed"] is True
    assert (ctx.case_dir / "judge.json").exists()


def test_某轮失败后停止后续轮次注入(short_tmp) -> None:
    agent = TurnEndAgent(session_id="sid-stop", reason_type="error", reason_text="boom")
    ctx, result = run_case(short_tmp, CASES_TWO_TURNS, agent=agent)

    assert agent.sent == ["记录午饭"]            # 第二轮未注入
    assert result["passed"] is None
    assert "运行未正常结束" in result["reason"]


# ---------------- 用了哪些 agent / 模型 ----------------


def test_结果标出用了哪些agent与模型(tmp_path) -> None:
    _, result = run_case(tmp_path, CASES_JUDGE_NONE)

    assert result["agents"] == {"under_test": True, "simulator": False, "judge": False}
    # 没启用的记 none；启用但 session 里取不到模型名的记 unknown
    assert result["models"]["judge"] == MODEL_NOT_USED
    assert result["models"]["under_test"] == MODEL_UNKNOWN
    assert result["models"]["simulator"] == MODEL_NOT_USED


def test_collect_agent_info_能从session里取到模型名(tmp_path) -> None:
    case = load_case(tmp_path, CASES_MIN)
    case_dir = tmp_path / "cdir"
    write_session(
        case_dir,
        [
            {"type": "request/header", "data": {"model_name": "doubao-seed-1-6"}},
            *SESSION_LINES,
        ],
    )

    agents, models = collect_agent_info(case, case_dir)

    assert agents["judge"] is True
    assert models["under_test"] == "doubao-seed-1-6"
    assert models["judge"] == MODEL_UNKNOWN


# ---------------- 用例级兜底 ----------------


def test_用例级异常被兜住并记进结果(tmp_path) -> None:
    def boom(**kwargs):
        raise RuntimeError("factory 挂了")

    ctx, _ = make_context(tmp_path, CASES_MIN)
    result = CaseExecutor(agent_factory=boom).run(ctx)

    assert result["error"] == "RuntimeError: factory 挂了"
    assert result["passed"] is None
    assert result["case_id"] == "T-1"     # 结果行仍然完整，便于归因
    # 异常逃到 run() 兜底这一层，同样要留现场（agent 工厂挂掉是最该看环境的一类）
    assert (ctx.case_dir / "env").is_dir()


# ---------------- TurnCollector ----------------


def test_turn_collector_记录多step正文并忽略非字符串content() -> None:
    service = EventService()
    collector = TurnCollector(service)

    service.emit(
        SESSION_EVENT.name,
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
    service.emit(
        SESSION_EVENT.name,
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
    service.emit(
        SESSION_EVENT.name,
        SessionEventPayload(
            session_record=SessionRecordData(
                type="turn/end",
                seq=3,
                data=TurnEndData(reason_type="success", reason_text="", error_type=""),
            )
        ),
    )
    asyncio.run(collector.wait_turn(0.5))            # 已 set，立即返回
    assert collector.turn_ends == ["success"]
    assert collector.last_assistant_text == "第一段"
