"""评测执行入口（runner）。

职责：按用例执行评测——重置状态 → 跑 under_test（+ 可选 simulator）→ 落盘 / 复制改名
session → 导出 evidence → 跑 judge → 跑 stats → 汇总落盘。

流程与约定见 [evalue/README.md](./README.md) 与
[lifeprismTestData/README.md](../../../lifeprismTestData/README.md)。

现状：已实现 a~f（重置状态 → precondition → 用例快照 → 跑 under_test → 取终时）。
g~k（session 落盘 / evidence / judge / stats）仍为 TODO：`_run_case` 会先返回
"未判"的部分结果（`passed=None`），待后续补齐。
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import json
import logging
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from lifeprismevalue.evalue.caseload import load_case_set
from lifeprismevalue.evalue.types import Case, CaseSet

logger = logging.getLogger(__name__)

# 每个用例运行前需还原的可变状态（相对 base/ 的路径）。
# DB 会累积记录、custom_prompt.md 会被写入规则，故必须还原，否则跨用例污染。
MUTABLE_PATHS: tuple[str, ...] = (
    "dataset/lifewatch_ai.db",
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

# 会话在 ctx.sessions 里的键（步骤 g 复制改名时用）
UNDER_TEST = "under_test"

# 单轮等待 turn/end 的超时（秒）：避免用例卡死
DEFAULT_TURN_TIMEOUT = 300.0

# g~k 未实现时，写入"测试结果摘要"的说明（是否通过留空 = 未判）
_PENDING_NOTE = "TODO: 步骤 g~k（session 落盘/evidence/judge/stats）未实现，暂未判定"


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
        self._event_service.register(SESSION_EVENT_NAME, self._on_event)

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


# SESSION_EVENT 的事件名（延迟导入：避免 runner 在无 myagent 环境下无法 import）
def _session_event_name() -> str:
    from myagent.infra.events.eventspec import SESSION_EVENT

    return SESSION_EVENT.name


SESSION_EVENT_NAME = _session_event_name()


# ---------------- 主类 ----------------


class EvalRunner:
    """按用例执行评测。"""

    def __init__(
        self,
        *,
        base_dir: str | Path,
        runs_dir: str | Path,
        agent_factory: Callable[..., Any] | None = None,
        turn_timeout: float = DEFAULT_TURN_TIMEOUT,
    ) -> None:
        """
        Args:
            base_dir: 共用底座目录（lifeprismData 的一份复制，只读）。
            runs_dir: 结果根目录；每次 run 在其下新建 <run_id>/。
            agent_factory: 创建被测评 agent 的工厂（默认 lifeprism 复刻 agent）；
                测试可注入假 agent。
            turn_timeout: 单轮等待 turn/end 的超时秒数。
        """
        self.base_dir = Path(base_dir)
        self.runs_dir = Path(runs_dir)
        self._agent_factory = agent_factory or default_under_test_agent_factory
        self.turn_timeout = turn_timeout

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

        已实现 a~f；g~k（session 落盘、evidence、judge、stats）仍为 TODO，
        故先返回 `passed=None` 的部分结果。
        """
        case_dir = self._case_dir(ctx, case_set, index, case)
        case_dir.mkdir(parents=True, exist_ok=True)

        self._reset_state(ctx, case)          # a. 重置可变状态
        self._apply_precondition(ctx, case)   # b. 写入 precondition.rules
        self._snapshot_case(case, case_dir)   # c. case.yaml 快照
        started_at = self._now()              # d. 时间窗起点

        session_id = await self._run_under_test(ctx, case, case_dir)  # e. 跑 under_test
        ended_at = self._now()                # f. 时间窗终点

        # g~k 未实现
        return CaseResult(
            case_id=case.id,
            case_type=case.type,
            case_dir=str(case_dir),
            version=case_set.meta.id,
            content_summary=_content_summary(case),
            session_id=session_id,
            passed=None,
            reason=_PENDING_NOTE,
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
        for turn in case.turns:
            if turn.trigger:
                raise NotImplementedError(
                    f"TODO: 带 trigger 的条件注入尚未实现（用例 {case.id}）"
                )
            await self._send_and_wait(agent, collector, turn.text)

    async def _send_and_wait(
        self, agent: Any, collector: TurnCollector, text: str
    ) -> None:
        """发送一轮用户消息并等待本轮 turn/end。"""
        collector.expect_turn()
        await agent.send(text)
        try:
            await collector.wait_turn(self.turn_timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"等待 turn/end 超时（{self.turn_timeout}s）") from e

    def _dump_sessions(self, ctx: RunContext, case: Case, case_dir: Path) -> None:
        """g. 先 flush 落盘，再把三个 agent 的 session 按语义名复制进 `case_dir`。

        产出（存在才写）：`session_under_test.jsonl` / `session_simulator.jsonl` / `session_judge.jsonl`。
        注意：session 文件名固定为 `<session_id>.jsonl` 且不支持改名，只能跑完复制改名；
        `SessionPresist` 每 2s 批量落盘，复制前必须 flush，否则丢最后一批。
        """
        raise NotImplementedError("TODO: 复制改名 session")

    def _export_evidence(
        self, ctx: RunContext, case: Case, t_start: str, t_end: str, case_dir: Path
    ) -> dict:
        """h. 导出 `evidence.json`。

        - `evidence` 取值是数据表：按 `created_at ∈ [t_start, t_end]` 从库中筛出本次写入；
        - 取值是 `.md` 文件：无时间戳，用运行前/后快照 diff 或取终态（日记路径解析成当天日期）。
        返回的证据 dict 同时交给 judge 与 stats。
        """
        raise NotImplementedError("TODO: 导出 evidence")

    def _run_judge(self, ctx: RunContext, case: Case, evidence: dict, case_dir: Path) -> dict | None:
        """i. `judge.mode=model` 时运行裁判 agent，产出 `judge.json`。

        输入 = 裁判提示词（agent 内建）+ `case.rubric` + `evidence` + under_test session 的
        user/assistant/tool_result 文本；输出 `{"pass": bool, "reason": str, ...}`。
        `judge.mode=none` 返回 None（仅留证据，不判）。
        """
        raise NotImplementedError("TODO: 运行 judge")

    def _run_stats(self, case: Case, case_dir: Path) -> dict:
        """j. 对被测评 session 跑通用统计组件，产出 `stats.json`（token / 耗时 / 路径）。"""
        raise NotImplementedError("TODO: 运行 stats")

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


# ---------------- 便捷入口 ----------------


async def run_cases(
    cases_path: str | Path, *, base_dir: str | Path, runs_dir: str | Path
) -> list[CaseResult]:
    """一次性执行一个 cases.yaml，返回各用例结果。"""
    return await EvalRunner(base_dir=base_dir, runs_dir=runs_dir).run(cases_path)
