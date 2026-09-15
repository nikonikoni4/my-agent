"""评测执行入口（runner）。

职责：按用例执行评测——重置状态 → 跑 under_test（+ 可选 simulator）→ 落盘 / 复制改名
session → 导出 evidence → 跑 judge → 跑 stats → 汇总落盘。

流程与约定见 [evalue/README.md](./README.md) 与
[lifeprismTestData/README.md](../../../lifeprismTestData/README.md)。

现状：a~j 全部实现（重置状态 → precondition → 用例快照 → 跑 under_test → 取终时 →
session 复制改名 → 导出 evidence → 跑裁判 → 统计）。仍未实现的只有任务模式相关的两处：
`input_mode=agent` 的 simulator 驱动、`turns[].trigger` 的条件注入。

运行终态：被测评 agent 每一轮的最终结果落在 `turn/end` 记录里（`TurnEndData`，含
reason_type / reason_text / error_type；后两者分别是异常类别与异常链文本）。loop 对
任何未恢复的异常都以 `error` 收口（含步数上限与重试耗尽，经 AgentUnclaimedError /
RetryExhaustedError），取消则为 `interrupted`。若某轮非 success，该用例**不判、不再
注入后续轮次**，以「运行未正常结束」收口（见 `_under_test_failure` / `_failure_note`）。
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import difflib
import json
import logging
import re
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from myagent.utils.helper import project_path_to_session_folder

from lifeprismevalue.evalue.caseload import load_case_set
from lifeprismevalue.evalue.types import Case, CaseSet
from lifeprismevalue.stats import analyze_session

logger = logging.getLogger(__name__)

# 数据库相对数据根的路径（lifeprism 的结构化存储）
DB_REL_PATH = "dataset/lifewatch_ai.db"

# 每个用例运行前需还原的可变状态（相对 base/ 的路径）。
# DB 会累积记录、custom_prompt.md 会被写入规则，故必须还原，否则跨用例污染。
MUTABLE_PATHS: tuple[str, ...] = (
    DB_REL_PATH,
    "agent/chat/custom_prompt.md",
)

# custom_prompt.md 里关联记录规则的章节标题（与 lifeprismData/agent/chat/agent.md 约定一致）
RULE_SECTION_TITLE = "## 关联记录规则"

# summary.csv 的列（见 lifeprismTestData/README.md「summary.csv 字段」）
SUMMARY_COLUMNS = [
    "时间",
    "版本",
    "session_id",
    "测试内容摘要",
    "测试结果摘要",
    "是否通过",
    "多轮(Y/N)",
    "会话开始时间",
    "会话结束时间",
]

# run 目录下的子目录：工作副本（agent 的数据根）与本次 run 的 session 落盘根
WORK_DIR_NAME = "work"
SESSIONS_DIR_NAME = "sessions"

# 会话在 ctx.sessions 里的键（步骤 g 复制改名时用），也是落盘文件名的后缀
UNDER_TEST = "under_test"
JUDGE = "judge"

# 单轮等待 turn/end 的超时（秒）：避免用例卡死
DEFAULT_TURN_TIMEOUT = 300.0

# 交给裁判的对话里，单条消息的最大字符数（超长则截断并标注）
TRANSCRIPT_MAX_CHARS = 4000

# 写进 summary 的「运行未正常结束」摘要最大字符数（异常链原文留在 session 里）
FAILURE_NOTE_MAX_CHARS = 500

# 统一 diff 的上下文行数（0 = 只留变更行）
DIFF_CONTEXT = 1

# 全树对比时跳过的大文件/二进制后缀（避免把 DB、会话、图片当文本比）
SKIP_SUFFIXES = frozenset(
    {
        ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3",
        ".jsonl", ".pyc", ".pyo",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf",
        ".zip", ".gz", ".exe", ".dll", ".so",
    }
)
MAX_TEXT_BYTES = 1_000_000


# ---------------- 运行时数据结构 ----------------


@dataclass
class RunContext:
    """一次 run 的运行上下文（路径与元信息）。"""

    run_id: str
    cases_path: Path
    base_dir: Path          # 共用底座 base/（只读）
    runs_dir: Path          # runs/
    run_dir: Path           # runs/<run_id>/
    work_data_path: Path    # runs/<run_id>/work/：agent 运行时的可写数据根
    session_folder: Path    # runs/<run_id>/sessions/：本次 run 的 session 落盘根
    sessions: dict[str, Any] = field(default_factory=dict)  # 键见 UNDER_TEST，供步骤 g 用


@dataclass
class CaseResult:
    """单条用例的执行结果（汇总到 summary 用）。"""

    case_id: str
    case_type: str
    case_dir: str
    version: str = ""           # 大类 id（meta.id）
    content_summary: str = ""   # 测试内容摘要
    session_id: str = ""        # 被测评 agent 的 session id
    passed: bool | None = None  # None = 未判（judge.mode=none 或判定步骤未实现）
    reason: str = ""
    multi_turn: bool = False
    started_at: str = ""
    ended_at: str = ""
    error: str = ""             # 用例级异常（不中断整个 run）


class TurnCollector:
    """订阅 session/event，等待 turn/end 并收集 assistant 正文。

    `ReActAgentLoop.send()` 只是把消息入队，turn 在后台跑；完成信号是
    session 记录里的 `turn/end`（session 每次 append 都会触发 session/event）。
    用法：`expect_turn()` → `send()` → `await wait_turn(timeout)`。

    注意：EventService 以弱引用持有回调，本实例必须由调用方强引用（driver 内为局部变量）。
    """

    def __init__(self, event_service: Any) -> None:
        self.turn_ends: list[str] = []          # 每轮 turn/end 的 reason_type
        self.assistant_texts: list[str] = []    # 各 step 的 assistant 正文（按序）
        self._done = asyncio.Event()
        self._event_service = event_service
        self._event_service.register(_session_event_name(), self._on_event)

    def _on_event(self, payload: Any) -> None:
        record = payload.session_record
        if record.type == "assistant/message":
            content = getattr(record.data.message, "content", "") or ""
            if isinstance(content, str) and content:
                self.assistant_texts.append(content)
        elif record.type == "turn/end":
            self.turn_ends.append(record.data.reason_type)
            self._done.set()

    def expect_turn(self) -> None:
        """发起新一轮前调用：清掉上一轮残留的完成信号。"""
        self._done.clear()

    async def wait_turn(self, timeout: float) -> None:
        """等待本轮 turn/end。"""
        await asyncio.wait_for(self._done.wait(), timeout)
        self._done.clear()

    @property
    def last_assistant_text(self) -> str:
        return self.assistant_texts[-1] if self.assistant_texts else ""


def _session_event_name() -> str:
    """session/event 的事件名（延迟导入 myagent，避免 runner 顶层拖入重依赖）。"""
    from myagent.infra.events.eventspec import SESSION_EVENT

    return SESSION_EVENT.name


# ---------------- 主类 ----------------


class EvalRunner:
    """按用例执行评测。"""

    def __init__(
        self,
        *,
        base_dir: str | Path,
        runs_dir: str | Path,
        agent_factory: Callable[..., Any] | None = None,
        judge_factory: Callable[..., Any] | None = None,
        turn_timeout: float = DEFAULT_TURN_TIMEOUT,
        scan_other_changed_files: bool = True,
    ) -> None:
        """
        Args:
            base_dir: 共用底座目录（lifeprismData 的一份复制，只读）。
            runs_dir: 结果根目录；每次 run 在其下新建 <run_id>/。
            agent_factory: 创建被测评 agent 的工厂（默认 lifeprism 复刻 agent）；
                测试可注入假 agent。
            judge_factory: 创建裁判 agent 的工厂（默认 judge_agent）；
                测试可注入假 agent。
            turn_timeout: 单轮等待 turn/end 的超时秒数。
            scan_other_changed_files: 导出证据时，是否额外全树对比、把"未被 evidence
                声明但确实改了的文本文件"也收进 evidence（防漏判）。
        """
        self.base_dir = Path(base_dir)
        self.runs_dir = Path(runs_dir)
        self._agent_factory = agent_factory or default_under_test_agent_factory
        self._judge_factory = judge_factory or default_judge_agent_factory
        self.turn_timeout = turn_timeout
        self.scan_other_changed_files = scan_other_changed_files

    # ---------- 顶层 ----------

    async def run(self, cases_path: str | Path) -> list[CaseResult]:
        """加载用例并逐条执行，返回本次 run 的各用例结果。

        单条用例异常不中断整个 run：异常记入该用例的 `CaseResult.error`。
        """
        case_set = load_case_set(cases_path)
        ctx = self._prepare_run(Path(cases_path), case_set)

        results: list[CaseResult] = []
        for index, case in enumerate(case_set.cases):
            try:
                results.append(await self._run_case(ctx, case_set, index, case))
            except Exception as e:  # noqa: BLE001 - 有意兜底，保证单条失败不影响其它用例
                logger.exception("用例 %s 执行失败", case.id)
                results.append(
                    CaseResult(
                        case_id=case.id,
                        case_type=case.type,
                        case_dir=str(self._case_dir(ctx, case_set, index, case)),
                        version=case_set.meta.id,
                        content_summary=_content_summary(case),
                        multi_turn=case.multi_turn,
                        error=f"{type(e).__name__}: {e}",
                    )
                )
            finally:
                # 无论成功失败都清掉本用例留在 ctx 上的信息：避免残留对象被下一条用例
                # 或后续步骤误用（典型：某用例在创建 agent 之前就失败，仍留着上一条的 agent）
                self._clear_case_state(ctx)
        self._write_summary(ctx, results)
        return results

    # ---------- 单条用例（对应 README 流程 a~k） ----------

    async def _run_case(
        self, ctx: RunContext, case_set: CaseSet, index: int, case: Case
    ) -> CaseResult:
        """执行一条用例。

        用例目录：`runs/<run_id>/<meta.id>/<index:03d>_<case.id>/`

        已实现 a~j。若被测评 agent 的某一轮以非 success 收场（`turn/end` 的终态），
        该用例**不判**、不重试，直接以「运行未正常结束」收口（错误信息取自 turn 终态）；
        证据与统计仍照常落盘，供事后排查。
        """
        case_dir = self._case_dir(ctx, case_set, index, case)
        case_dir.mkdir(parents=True, exist_ok=True)

        self._reset_state(ctx, case)          # a. 重置可变状态
        self._apply_precondition(ctx, case)   # b. 写入 precondition.rules
        self._snapshot_case(case, case_dir)   # c. case.yaml 快照
        started_at = self._now()              # d. 时间窗起点

        session_id = await self._run_under_test(ctx, case, case_dir)  # e. 跑 under_test
        ended_at = self._now()                # f. 时间窗终点

        self._dump_sessions(ctx, case, case_dir)                              # g. session 落盘
        failure = _under_test_failure(                                        # 运行终态
            _read_turn_results(case_dir / f"session_{UNDER_TEST}.jsonl")
        )
        evidence = self._export_evidence(ctx, case, started_at, ended_at, case_dir)  # h. 证据
        verdict = (
            await self._run_judge(ctx, case, evidence, case_dir)              # i. 裁判
            if failure is None
            else None
        )
        self._run_stats(case, case_dir)                                       # j. 统计
        # 再落一次 session：judge 的会话在 i 之后才产生，也要进用例目录（复制循环幂等）
        self._dump_sessions(ctx, case, case_dir)

        if failure is not None:
            note = _failure_note(failure)
            return CaseResult(
                case_id=case.id,
                case_type=case.type,
                case_dir=str(case_dir),
                version=case_set.meta.id,
                content_summary=_content_summary(case),
                session_id=session_id,
                passed=None,
                reason=note,
                error=note,
                multi_turn=case.multi_turn,
                started_at=started_at,
                ended_at=ended_at,
            )

        if verdict is None:
            passed: bool | None = None
            reason = f"judge.mode={case.judge.mode}，未判定"
        else:
            passed = verdict["pass"]
            reason = verdict["reason"] or verdict["parse_error"]

        return CaseResult(
            case_id=case.id,
            case_type=case.type,
            case_dir=str(case_dir),
            version=case_set.meta.id,
            content_summary=_content_summary(case),
            session_id=session_id,
            passed=passed,
            reason=reason,
            multi_turn=case.multi_turn,
            started_at=started_at,
            ended_at=ended_at,
        )

    # ---------- 各步骤 ----------

    def _reset_state(self, ctx: RunContext, case: Case) -> None:
        """a. 把可变状态还原到基线（从 base 覆盖 work 里的可变路径）。"""
        for rel in MUTABLE_PATHS:
            src = ctx.base_dir / rel
            if not src.exists():
                continue
            dst = ctx.work_data_path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    def _apply_precondition(self, ctx: RunContext, case: Case) -> None:
        """b. 把 `case.precondition.rules` 写入 `custom_prompt.md`（无规则则不动）。"""
        rules = case.precondition.rules
        if not rules:
            return
        path = ctx.work_data_path / "agent" / "chat" / "custom_prompt.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(_upsert_rule_section(text, rules), encoding="utf-8")

    def _snapshot_case(self, case: Case, case_dir: Path) -> None:
        """c. 把该用例的定义快照到 `case_dir/case.yaml`（保证 run 自包含）。"""
        case_dir.mkdir(parents=True, exist_ok=True)
        data = asdict(case)
        (case_dir / "case.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    async def _run_under_test(self, ctx: RunContext, case: Case, case_dir: Path) -> str:
        """e. 运行被测评 agent，返回其 session_id。

        当前只实现 `input_mode=scripted`（按 `turns` 顺序注入）；
        `input_mode=agent`（simulator 驱动）留待后续。

        收尾固定两件事：先 flush 落盘（`persist_session_now`），再 `cancel()` 停掉后台循环。
        """
        if case.uses_simulator:
            raise NotImplementedError(f"TODO: input_mode=agent 的 simulator 驱动（用例 {case.id}）")

        agent = self._agent_factory(
            data_path=ctx.work_data_path,
            session_folder=ctx.session_folder,
            # 注意：不要传 name——lifeprism 复刻 agent 的 SystemPrompt 按固定名注册，
            # 改 name 会导致提示词查不到（提示词为空）。
        )
        ctx.sessions[UNDER_TEST] = agent
        collector = TurnCollector(agent._event_service)
        try:
            await self._drive_scripted(agent, collector, case)
        finally:
            agent.persist_session_now()
            agent.cancel()

        return agent._session.meta_data.session_id

    async def _drive_scripted(self, agent: Any, collector: TurnCollector, case: Case) -> None:
        """按 `turns` 顺序逐轮注入（带 `trigger` 的条件注入尚未实现）。"""
        if not case.turns:
            logger.warning("用例 %s 没有 turns，未执行任何一轮", case.id)
            return
        for index, turn in enumerate(case.turns, start=1):
            if turn.trigger:
                raise NotImplementedError(
                    f"TODO: 带 trigger 的条件注入尚未实现（用例 {case.id}）"
                )
            await self._send_and_wait(agent, collector, turn.text)
            # 某轮以非 success 收场就停止后续注入：turn 的异常会击穿 loop，
            # 此后 send 会重启已终止的循环，把消息灌进已失败的运行。
            reason = collector.turn_ends[-1] if collector.turn_ends else ""
            if reason and reason != "success":
                logger.warning(
                    "用例 %s 的第 %d 轮以 %s 收场，停止后续轮次注入", case.id, index, reason
                )
                break

    async def _send_and_wait(
        self, agent: Any, collector: TurnCollector, text: str
    ) -> None:
        """发送一轮用户消息并等待本轮 turn/end。"""
        # TODO: 超时路径与「运行未正常结束」不一致——这里抛错会直接冒到 `run()` 的兜底，
        # 该用例只剩 `CaseResult.error`，g~j 不执行（无 evidence / stats）。
        # 待统一：超时也应走"运行未正常结束"收口，照样落证据与统计。
        collector.expect_turn()
        await agent.send(text)
        try:
            await collector.wait_turn(self.turn_timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"等待 turn/end 超时（{self.turn_timeout}s）") from e

    def _dump_sessions(self, ctx: RunContext, case: Case, case_dir: Path) -> None:
        """g. 把本次用例涉及的各 agent 的 session 复制进 `case_dir`，按语义名重命名。

        session 文件名固定为 `<session_id>.jsonl` 且不支持改名，只能跑完复制改名
        （见 lifeprismevalue/evalue/README.md）。文件不存在（如 agent 未落盘）时跳过并告警。
        """
        case_dir.mkdir(parents=True, exist_ok=True)
        for key, agent in ctx.sessions.items():
            session_id = _session_id_of(agent)
            if not session_id:
                logger.warning("会话 %s 取不到 session_id，跳过落盘", key)
                continue
            src = session_file_path(ctx.work_data_path, ctx.session_folder, session_id)
            if not src.exists():
                logger.warning("会话文件不存在，跳过落盘: %s", src)
                continue
            shutil.copy2(src, case_dir / f"session_{key}.jsonl")

    def _export_evidence(
        self, ctx: RunContext, case: Case, t_start: str, t_end: str, case_dir: Path
    ) -> dict:
        """h. 导出 `evidence.json`。

        - key 用用例 `evidence` 里的原值（表名 / 文件路径模式），便于与 cases.yaml 对照；
        - 表：按 `created_at ∈ [t_start, t_end]` 捞整行；
        - 文件：与 base 对比出统一 diff（`.md` 没有时间戳，靠 diff 而非时间窗）；
        - 额外做一次全树对比，收进"未被声明但确实改了"的文本文件（防漏判）。
        """
        targets = {raw: self._collect_target(ctx, raw, t_start, t_end) for raw in case.evidence}
        evidence = {
            "case_id": case.id,
            "time_window": {"start": t_start, "end": t_end},
            "precondition": {"rules": list(case.precondition.rules)},
            "targets": targets,
            "other_changed_files": (
                self._collect_other_changed_files(ctx, case.evidence)
                if self.scan_other_changed_files
                else {}
            ),
        }
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return evidence

    def _collect_target(self, ctx: RunContext, raw: str, t_start: str, t_end: str) -> dict:
        """按 evidence 取值分派到「表」或「文件」两类采集。"""
        if _is_file_target(raw):
            return self._collect_file(ctx, raw)
        return self._collect_table(ctx, raw, t_start, t_end)

    def _collect_table(self, ctx: RunContext, table: str, t_start: str, t_end: str) -> dict:
        """按时间窗捞表内新增行（无 `created_at` 的表退化为全表并注明）。"""
        db_path = ctx.work_data_path / DB_REL_PATH
        if not db_path.exists():
            return _table_result(table, [], error=f"数据库不存在: {DB_REL_PATH}")
        try:
            con = sqlite3.connect(db_path)
            try:
                cur = con.cursor()
                columns = [row[1] for row in cur.execute(f"PRAGMA table_info({table})")]
                if not columns:
                    return _table_result(table, [], error=f"表不存在: {table}")
                note = ""
                if "created_at" in columns:
                    sql = (
                        f"SELECT * FROM {table} WHERE created_at >= ? AND created_at <= ? "
                        "ORDER BY created_at"
                    )
                    rows = [
                        dict(zip(columns, r)) for r in cur.execute(sql, (t_start, t_end))
                    ]
                    # SQL 用字符串比较做粗筛（写入方统一用 ISO UTC），这里再按时间解析复核
                    rows = [r for r in rows if _in_window(r.get("created_at"), t_start, t_end)]
                else:
                    rows = [dict(zip(columns, r)) for r in cur.execute(f"SELECT * FROM {table}")]
                    note = f"该表无 created_at，已退化为全表（{len(rows)} 行）"
                return _table_result(table, rows, columns=columns, note=note)
            finally:
                con.close()
        except sqlite3.Error as e:
            return _table_result(table, [], error=f"读取失败: {e}")

    def _collect_file(self, ctx: RunContext, raw: str) -> dict:
        """采集文本类证据：解析路径占位符，与 base 对比出统一 diff。"""
        rel = _resolve_path_placeholders(raw)
        return _file_diff(ctx.base_dir / rel, ctx.work_data_path / rel, rel)

    def _collect_other_changed_files(self, ctx: RunContext, declared: Iterable[str]) -> dict:
        """全树对比 base 与 work，收集声明之外被改动的文本文件。"""
        declared_rel = {
            _resolve_path_placeholders(item) for item in declared if _is_file_target(item)
        }
        changed: dict[str, dict] = {}
        base_files = _iter_candidate_files(ctx.base_dir)
        work_files = _iter_candidate_files(ctx.work_data_path)
        for rel in sorted(set(base_files) | set(work_files)):
            if rel in declared_rel:
                continue
            item = _file_diff(ctx.base_dir / rel, ctx.work_data_path / rel, rel)
            if item["changed"]:
                changed[rel] = item
        return changed

    async def _run_judge(
        self, ctx: RunContext, case: Case, evidence: dict, case_dir: Path
    ) -> dict | None:
        """i. `judge.mode=model` 时运行裁判 agent，产出 `judge.json`；`none` 时返回 None。

        输入 = 裁判提示词（agent 内建）+ `case.rubric` + `evidence` + under_test 对话记录
        （从 g 复制出的 `session_under_test.jsonl` 抽取 user/assistant/tool_result）。
        输出 `{"pass": bool, "reason": str, ...}`；解析不了则 `pass=None` 并保留原文。
        """
        if not case.uses_judge:
            return None

        transcript = _read_transcript(case_dir / f"session_{UNDER_TEST}.jsonl")
        prompt_text = _build_judge_input(case, evidence, transcript)

        agent = self._judge_factory(
            data_path=ctx.work_data_path,
            session_folder=ctx.session_folder,
        )
        ctx.sessions[JUDGE] = agent
        collector = TurnCollector(agent._event_service)
        try:
            await self._send_and_wait(agent, collector, prompt_text)
        finally:
            agent.persist_session_now()
            agent.cancel()

        raw = collector.last_assistant_text
        verdict = {
            "case_id": case.id,
            "mode": case.judge.mode,
            "session_id": _session_id_of(agent),
            **_parse_verdict(raw),
            "raw": raw,
        }
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "judge.json").write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return verdict

    def _run_stats(self, case: Case, case_dir: Path) -> dict:
        """j. 对 g 复制出的 `session_under_test.jsonl` 跑通用统计组件，产出 `stats.json`。

        以文件为接口（而非内存里的 agent 对象），使 g~j 可解耦、可单独重跑。
        统计是辅助信息、不参与判定，故 session 缺失时只记进 `errors`，不抛出。
        """
        session_path = case_dir / f"session_{UNDER_TEST}.jsonl"
        if not session_path.is_file():
            logger.warning("用例 %s 的 session 文件不存在，跳过统计: %s", case.id, session_path)
            merged: dict = _stats_error(session_path, f"session 文件不存在: {session_path.name}")
        else:
            try:
                merged = analyze_session(session_path)
            except Exception as e:  # noqa: BLE001 - 统计是辅助信息，解析失败只记录，不拖垮用例
                logger.warning("用例 %s 统计失败: %s", case.id, e)
                merged = _stats_error(session_path, f"{type(e).__name__}: {e}")

        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "stats.json").write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return merged

    # ---------- 辅助 ----------

    def _prepare_run(self, cases_path: Path, case_set: CaseSet) -> RunContext:
        """建 `runs/<run_id>/`、从 base 复制一份工作副本、并写 `run.json`。

        - `work/` 是本次 run 内 agent 实际读写的数据根（base 保持只读）；
        - 用例之间只重置"可变状态"（见 `MUTABLE_PATHS`），不重复整份复制。
        """
        if not self.base_dir.is_dir():
            raise FileNotFoundError(f"base 目录不存在: {self.base_dir}")

        run_id = self._make_run_id()
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        work_data_path = run_dir / WORK_DIR_NAME
        shutil.copytree(self.base_dir, work_data_path, dirs_exist_ok=True)

        ctx = RunContext(
            run_id=run_id,
            cases_path=cases_path,
            base_dir=self.base_dir,
            runs_dir=self.runs_dir,
            run_dir=run_dir,
            work_data_path=work_data_path,
            session_folder=run_dir / SESSIONS_DIR_NAME,
        )
        run_meta = {
            "run_id": run_id,
            "started_at": self._now(),
            "cases_path": str(cases_path),
            "cases_id": case_set.meta.id,
            "dataset_version": case_set.meta.dataset_version,
            "base_dir": str(self.base_dir),
            "case_count": len(case_set.cases),
            # TODO: 溯源补齐 prompt / model / grader 版本（三者的版本号与校准结果）
        }
        (run_dir / "run.json").write_text(
            json.dumps(run_meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return ctx

    def _case_dir(self, ctx: RunContext, case_set: CaseSet, index: int, case: Case) -> Path:
        """用例目录：`runs/<run_id>/<meta.id>/<index:03d>_<case.id>/`。"""
        return ctx.run_dir / case_set.meta.id / f"{index:03d}_{case.id}"

    @staticmethod
    def _clear_case_state(ctx: RunContext) -> None:
        """清空 ctx 中与"当前用例"相关的信息（一条用例跑完后调用）。

        目前只有 `sessions`（被测评 / 模拟用户 / 裁判 agent 的引用）。清掉可避免
        "某用例在创建 agent 之前就失败"时，残留的上一条用例对象被后续步骤误用。
        以后若 ctx 上新增用例级字段，一并在此清理。
        """
        ctx.sessions.clear()

    def _write_summary(self, ctx: RunContext, results: list[CaseResult]) -> None:
        """写本次 run 的 `summary.csv`，并追加到全局 `runs/summary.csv`。"""
        rows = [_summary_row(r) for r in results]
        _write_csv(ctx.run_dir / "summary.csv", rows)
        _append_csv(ctx.runs_dir / "summary.csv", rows)

    @staticmethod
    def _now() -> str:
        """当前 UTC 时间（ISO 8601），用于时间窗。"""
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    @staticmethod
    def _make_run_id() -> str:
        """run_id：UTC 时间戳 `YYYYmmdd-HHMMSS`。"""
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


# ---------------- 模块级辅助 ----------------


def default_under_test_agent_factory(**kwargs: Any) -> Any:
    """默认的被测评 agent：lifeprism 复刻 agent（延迟导入，避免拖入重依赖）。"""
    from lifeprismevalue.agent.old_lifeprism_agent import create_old_agent

    return create_old_agent(**kwargs)


def default_judge_agent_factory(**kwargs: Any) -> Any:
    """默认的裁判 agent：judge_agent（延迟导入）。"""
    from lifeprismevalue.agent.judge_agent import create_judge_agent

    return create_judge_agent(**kwargs)


def _stats_error(session_path: Path, message: str) -> dict:
    """统计无法进行时的占位结果：保持与 StatsRunner 输出同形（components / errors）。"""
    return {
        "session_path": str(session_path),
        "components": {},
        "errors": {"StatsRunner": message},
    }


def _truncate(text: str, limit: int = TRANSCRIPT_MAX_CHARS) -> str:
    """超长文本截断并标注（避免把整段长文塞给裁判）。"""
    return text if len(text) <= limit else text[:limit] + "…（已截断）"


def _message_text(message: Any) -> str:
    """从 session 记录的 `message` 抽正文：`content` 可能是字符串或分段列表。"""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return _truncate(content)
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                parts.append(part)
        return _truncate("".join(parts))
    return ""


def _read_turn_results(session_path: Path) -> list[dict]:
    """从 session jsonl 读出每一轮的终态（`turn/end`）。

    这是"本轮跑成什么样"的唯一权威来源：`TURN_END` 事件的 payload 不带信息，
    而 `turn/end` 记录（`TurnEndData`）里有本轮 `FinalResult` 的
    `reason_type` / `reason_text` / `error_type`——`error_type` 是最外层异常的类名
    （如 AgentUnclaimedError / RetryExhaustedError / MaxStepsExceededError），
    `reason_text` 是异常链文本（逐层 `类型: 消息`）。

    字段用 `.get` 读、逐行容错，避免与 myagent 的 schema 演进强耦合。
    """
    if not session_path.is_file():
        return []
    results: list[dict] = []
    for raw_line in session_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "turn/end":
            continue
        data = record.get("data") or {}
        results.append(
            {
                "turn": record.get("turn"),
                "reason_type": str(data.get("reason_type") or ""),
                "reason_text": str(data.get("reason_text") or ""),
                "error_type": str(data.get("error_type") or ""),
            }
        )
    return results


def _under_test_failure(turn_results: list[dict]) -> dict | None:
    """取第一条非 success 的 turn 终态（error / interrupted）；都正常则返回 None。

    只报告、不重试：运行本身没跑完的用例，其结论不成立，不能当成"agent 记错了"计分。
    """
    for item in turn_results:
        if item["reason_type"] and item["reason_type"] != "success":
            return item
    return None


def _failure_note(turn: dict) -> str:
    """把 turn 的异常终态整理成一句可读摘要（写进 summary 的"测试结果摘要"）。

    `error_type` 是异常类别（最外层异常类名），`reason_text` 是异常链文本
    （逐层 `类型: 消息`，见 loop 的 `_format_error_chain`），两者互补。
    """
    head = f"运行未正常结束（turn {turn['turn']} · {turn['reason_type']}）"
    detail = "｜".join(part for part in (turn["error_type"], turn["reason_text"]) if part)
    return f"{head}：{_truncate(detail, FAILURE_NOTE_MAX_CHARS)}" if detail else head


def _read_transcript(session_path: Path) -> str:
    """从 session jsonl 抽出 user / assistant / tool_result 三类消息，拼成交给裁判的对话文本。

    直接按 jsonl 的字段解析（不依赖 session 的类型定义），避免 runner 反向依赖 myagent 内部结构。
    """
    if not session_path.is_file():
        return ""
    lines: list[str] = []
    for raw_line in session_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        record_type = record.get("type")
        data = record.get("data") or {}
        if record_type == "user/message":
            lines.append(f"[user] {_message_text(data.get('message'))}")
        elif record_type == "assistant/message":
            lines.append(f"[assistant] {_message_text(data.get('message'))}")
        elif record_type == "tool/result":
            lines.append(
                f"[tool_result:{data.get('tool_name', '')}] {_message_text(data.get('message'))}"
            )
    return "\n".join(lines)


def _build_judge_input(case: Case, evidence: dict, transcript: str) -> str:
    """把「判分要点 + 本次证据 + 对话记录」拼成给裁判的一条 user 消息。"""
    return "\n\n".join(
        [
            "# 判分要点\n" + case.rubric.strip(),
            "# 本次证据\n" + json.dumps(evidence, ensure_ascii=False, indent=2),
            "# 对话记录\n" + (transcript or "（无）"),
        ]
    )


def _parse_verdict(raw: str) -> dict:
    """从裁判输出里抽出 `{"pass": bool, "reason": str}`。

    容错点（LLM 输出是外部边界）：剥掉 ``` 围栏、只取最外层 JSON、`pass` 允许是
    "true"/"通过" 这类字符串。确实解析不出时 `pass=None`，把原文留给人工看。
    """
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {"pass": None, "reason": "", "parse_error": "裁判输出中未找到 JSON"}
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        return {"pass": None, "reason": "", "parse_error": f"JSON 解析失败: {e}"}

    passed = data.get("pass")
    if isinstance(passed, str):
        passed = passed.strip().lower() in {"true", "yes", "y", "通过", "达标"}
    if not isinstance(passed, bool):
        return {
            "pass": None,
            "reason": str(data.get("reason", "")),
            "parse_error": "缺少布尔字段 pass",
        }
    return {"pass": passed, "reason": str(data.get("reason", "")), "parse_error": ""}


def _content_summary(case: Case) -> str:
    """测试内容摘要：用例编号 + 类型（多轮再标注）。"""
    suffix = "（多轮）" if case.multi_turn else ""
    return f"{case.id}·{case.type}{suffix}"


def _upsert_rule_section(text: str, rules: list[str]) -> str:
    """把关联记录规则写入 `## 关联记录规则` 章节：有则替换其正文，无则追加章节。"""
    body = "\n".join(f"{i}. {rule}" for i, rule in enumerate(rules, 1))
    lines = text.splitlines()

    start = next((i for i, ln in enumerate(lines) if ln.strip() == RULE_SECTION_TITLE), None)
    if start is None:
        prefix = text.rstrip("\n")
        sep = "\n\n" if prefix else ""
        return f"{prefix}{sep}{RULE_SECTION_TITLE}\n{body}\n"

    # 章节结束 = 下一个 '## ' 开头的行（或文末）
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
    return "\n".join(lines[:start] + [RULE_SECTION_TITLE, body] + lines[end:]) + "\n"


def _summary_row(result: CaseResult) -> dict[str, str]:
    """把 CaseResult 映射为 summary.csv 的一行。"""
    passed = "" if result.passed is None else ("Y" if result.passed else "N")
    return {
        "时间": result.ended_at or result.started_at,
        "版本": result.version,
        "session_id": result.session_id,
        "测试内容摘要": result.content_summary,
        "测试结果摘要": result.reason,
        "是否通过": passed,
        "多轮(Y/N)": "Y" if result.multi_turn else "N",
        "会话开始时间": result.started_at,
        "会话结束时间": result.ended_at,
    }


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    """覆盖写 CSV（含表头）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _append_csv(path: Path, rows: list[dict[str, str]]) -> None:
    """追加写 CSV（文件不存在时先写表头）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


# ---------------- 证据采集辅助 ----------------


def _is_file_target(value: str) -> bool:
    """evidence 取值是"文件"还是"表"：有路径分隔符或以 .md 结尾的按文件处理。"""
    return value.endswith(".md") or "/" in value or "\\" in value


def _session_id_of(agent: Any) -> str:
    """从 agent 上取 session_id（agent._session.meta_data.session_id）。"""
    session = getattr(agent, "_session", None)
    meta = getattr(session, "meta_data", None)
    return getattr(meta, "session_id", "") or ""


def session_file_path(work_data_path: Path, session_folder: Path, session_id: str) -> Path:
    """按 session_id + 数据根定位 session 文件（与 SessionStore 的规则一致）。"""
    return project_path_to_session_folder(work_data_path, session_folder) / f"{session_id}.jsonl"


def _resolve_path_placeholders(raw: str) -> str:
    """把 evidence 里的 `<year>/<month>/<date>` 占位符解析为具体日期（按本地日期）。"""
    today = datetime.datetime.now()
    return (
        raw.replace("<year>", f"{today:%Y}")
        .replace("<month>", f"{today:%m}")
        .replace("<date>", f"{today:%Y-%m-%d}")
    )


def _read_text(path: Path) -> str | None:
    """读取文本文件；不存在或非 UTF-8 文本时返回 None。"""
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _in_window(value: Any, t_start: str, t_end: str) -> bool:
    """`created_at` 是否落在时间窗内。

    SQL 已用字符串粗筛（写入方统一 ISO UTC 格式）；这里按时间解析复核。
    解析失败的保守保留（宁可多给裁判一行，也不静默丢证据）。
    """
    if not isinstance(value, str):
        return True
    try:
        moment = datetime.datetime.fromisoformat(value)
        return datetime.datetime.fromisoformat(t_start) <= moment <= datetime.datetime.fromisoformat(t_end)
    except ValueError:
        return True


def _table_result(
    table: str,
    rows: list[dict],
    columns: list[str] | None = None,
    note: str = "",
    error: str = "",
) -> dict:
    """组装一条"表类证据"结果。"""
    return {
        "kind": "table",
        "table": table,
        "columns": columns or [],
        "rows": rows,
        "row_count": len(rows),
        "note": note,
        "error": error,
    }


def _file_diff(base_path: Path, current_path: Path, rel: str) -> dict:
    """对比同一相对路径在 base 与 work 中的文本，产出统一 diff 结构。"""
    current_text = _read_text(current_path)
    base_text = _read_text(base_path)
    exists = current_text is not None
    new_file = exists and base_text is None

    result = {
        "kind": "file",
        "resolved_path": rel,
        "exists": exists,
        "changed": False,
        "new_file": new_file,
        "diff": "",
        "added_lines": 0,
        "removed_lines": 0,
    }
    if not exists:
        return result

    before = base_text if base_text is not None else ""
    if before == current_text:
        return result

    diff_lines = list(
        difflib.unified_diff(
            before.splitlines(),
            current_text.splitlines(),
            fromfile="base",
            tofile="current",
            lineterm="",
            n=DIFF_CONTEXT,
        )
    )
    result["changed"] = True
    result["diff"] = "\n".join(diff_lines)
    result["added_lines"] = sum(
        1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
    )
    result["removed_lines"] = sum(
        1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
    )
    return result


def _iter_candidate_files(root: Path) -> set[str]:
    """遍历目录下的候选文本文件，返回相对 posix 路径集合（跳过二进制/超大文件）。"""
    if not root.is_dir():
        return set()
    found: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_TEXT_BYTES:
                continue
        except OSError:
            continue
        found.add(path.relative_to(root).as_posix())
    return found


# ---------------- 便捷入口 ----------------


async def run_cases(
    cases_path: str | Path, *, base_dir: str | Path, runs_dir: str | Path
) -> list[CaseResult]:
    """一次性执行一个 cases.yaml，返回各用例结果。"""
    return await EvalRunner(base_dir=base_dir, runs_dir=runs_dir).run(cases_path)
