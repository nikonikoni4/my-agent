"""单条用例的执行实体（领域层）：读用例 → 跑 under_test → 落盘 → 取证 → 判 → 统计。

**为什么单独成模块**：它对 `evaluate.core` 是一份**不透明的载荷**——core 只知道
`WorkerTask(entrypoint, payload)`，这条任务到底是什么，只有本模块的
`execute_case(payload, env)` 知道。core 与 provider 都不认识「用例」「判分」，这是解耦的
关键：换测试内容只换 entrypoint，core 与 provider 一行不动。

**跑在哪**：子进程（见 [worker.py](./worker.py)），`execute_case` 就是子进程侧的
entrypoint。它做的第一件事是把数据根切到 `env.root`——工具侧取库只经过
`config.get_db_path()` 这个模块级全局，不切根就会写进真实 `lifeprismData`：写入
「成功」却落在别处，而证据读的是环境，于是每条用例都「无新增」，且不会有任何报错。

**流程**（`evalue/README.md` 的 a~j）：

| 步 | 做什么 | 谁做 |
| --- | --- | --- |
| a | 打基线快照（环境初始态，**在 precondition 之前**） | `_snapshot_baseline` |
| b | 应用 precondition（规则写进 `custom_prompt.md`） | `_apply_precondition` |
| c | 用例快照 → `case.yaml` | `_snapshot_case` |
| d~f | 跑 under_test（scripted 注入），取 turn 终态 | `_run_under_test` / `_run_turn` |
| g | session 复制改名进用例目录 | `_dump_sessions` |
| h | 导出 `evidence.json`（与环境基线对比） | `evidence.collect_evidence` |
| i | 裁判 → `judge.json` | `_run_judge` |
| j | 统计 → `stats.json` | `_run_stats` |
| 收尾 | 回执 → `result.json`（**只装别处推不出来的三样**，见 `_write_result`） | `_write_result` |

「重置可变状态」不在本模块：环境每用例独占，槽位复用前由 core 的 `reset_env` 整份回滚。

**失败现场**：「运行未正常结束」或「判定不通过」时，跑完后的环境会整份留到
`<用例目录>/env/`，与 a 步的 `baseline/`（跑之前）成对——`diff -r` 一下就是本次改动。
core 的 `keep_env_on_failure` 只管「执行通道失败」（子进程起不来/崩了/没产出结果），
这两类在 core 眼里是**成功**（子进程 exit 0），靠它留不下来；而恰恰是这两类最需要现场。

**运行终态**：每一轮的最终结果落在 `turn/end` 记录里（`TurnEndData`，含
`reason_type` / `reason_text` / `error_type`）。loop 对任何未恢复的异常都以 `error` 收口
（含步数上限、重试耗尽），取消为 `interrupted`。某轮非 success 时该用例**不判、不重试、
不再注入后续轮次**，以「运行未正常结束」收口（证据与统计仍照常落盘，供排查）。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import math
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from myagent.utils.helper import project_path_to_session_folder

from lifeprismevalue import config
from lifeprismevalue.evalue.caseload import load_case_set
from lifeprismevalue.evalue.evidence import collect_evidence, snapshot_baseline
from lifeprismevalue.evalue.types import (
    CASE_FILENAME,
    EVIDENCE_FILENAME,
    JUDGE_FILENAME,
    RESULT_FILENAME,
    STATS_FILENAME,
    Case,
)
from lifeprismevalue.stats import analyze_session

logger = logging.getLogger(__name__)

# 单轮等待 turn/end 的超时（秒）：避免用例卡死
DEFAULT_TURN_TIMEOUT = 300.0

# 交给裁判的对话里，单条消息的最大字符数（超长则截断并标注）
TRANSCRIPT_MAX_CHARS = 4000

# 写进 summary 的「运行未正常结束」摘要最大字符数（异常链原文留在 session 里）
FAILURE_NOTE_MAX_CHARS = 500

# custom_prompt.md 里关联记录规则的章节标题（与 lifeprismData/agent/chat/agent.md 约定一致）
RULE_SECTION_TITLE = "## 关联记录规则"

# 会话在 ctx.sessions 里的键（step g 复制改名时用），也是落盘文件名的后缀
UNDER_TEST = "under_test"
SIMULATOR = "simulator"
JUDGE = "judge"

# 跑**之后**的环境副本放在用例目录下的这个子目录（只在失败时留，见 `_keep_env`）。
# 与 runner 放进 payload 的"跑**之前**"快照（`baseline/`）成对，一比就是本次改动。
# 基线目录名由 runner 定（它算路径），这里只声明本模块自己写的那个，免得两处各写一份。
ENV_DIR_NAME = "env"

# 模型名的两个缺省取值：本次没用该 agent / 用了但 session 里取不到
MODEL_NOT_USED = "none"
MODEL_UNKNOWN = "unknown"


# ---------------- 运行时上下文 ----------------


@dataclass
class CaseContext:
    """跑一条用例需要的全部输入（路径 + 用例 + 参数）。

    每条用例一份，用例之间不共享——上一版把用例级状态挂在 run 级 ctx 上、跑完再清，
    这里天然没有这个问题（也不会有「上一条用例的 agent 残留」）。
    """

    case: Case
    meta_id: str               # 大类 id（CaseResult.version）
    case_dir: Path             # runs/<run_id>/<meta.id>/<NNN>_<case.id>/
    env_root: Path             # 本次用例的环境数据根（= 数据根，子进程已切到此）
    baseline_dir: Path         # 本用例环境的初始态快照（取证时对比用）
    db_rel_path: str           # 库在数据根里的相对路径；空串 = 本环境没声明库
    session_folder: Path       # 会话落盘根（runs/<run_id>/sessions/）
    turn_timeout: float = DEFAULT_TURN_TIMEOUT
    scan_other_changed_files: bool = True
    sessions: dict[str, Any] = field(default_factory=dict)  # 本用例用到的各角色 agent
    started_at: str = ""       # 会话开始时间（d）
    ended_at: str = ""         # 会话结束时间（f）


class TurnCollector:
    """订阅 session/event，等待 turn/end 并收集 assistant 正文。

    `ReActAgentLoop.send()` 只是把消息入队，turn 在后台跑；完成信号是 session 记录里的
    `turn/end`（session 每次 append 都会触发 session/event）。
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


# ---------------- 执行实体 ----------------


class CaseExecutor:
    """把一条用例跑完，产出一份 `CaseResult` 形状的字典。

    agent 工厂可注入（测试用假 agent）：真跑时用默认工厂，自动接上 lifeprism 复刻 agent
    与裁判 agent；这两个默认工厂都在创建时才 import，避免顶层拖入重依赖。
    """

    def __init__(
        self,
        *,
        agent_factory: Callable[..., Any] | None = None,
        judge_factory: Callable[..., Any] | None = None,
    ) -> None:
        """
        Args:
            agent_factory: 创建被测评 agent 的工厂（默认 lifeprism 复刻 agent）。
            judge_factory: 创建裁判 agent 的工厂（默认 judge_agent）。
        """
        self._agent_factory = agent_factory or default_under_test_agent_factory
        self._judge_factory = judge_factory or default_judge_agent_factory

    def run(self, ctx: CaseContext) -> dict[str, Any]:
        """跑一条用例；用例级异常在这里收口，不往上冒。

        收口的意义：一条用例崩了不该让整个 run 失去它的结果行，也不该丢掉已经落盘的
        证据——异常记进 `CaseResult.error`，产物目录里跑到哪算哪（与「运行未正常结束」
        同一套收口思路）。异常逃到这一层时也把环境留下（`_keep_env`）：这正是最该看
        现场的一类（agent 工厂挂了、超时、走到未实现的路径）。

        出口只有这一个，`result.json` 也在这里落——**每条用例目录必有一份**（真跑起来的
        用例由本函数写，子进程压根没起来的由 runner 补），重建 summary 时才不用去猜
        「这条是没跑，还是没落盘」。
        """
        try:
            result = asyncio.run(self._run_async(ctx))
        except Exception as e:  # noqa: BLE001 - 有意兜底：单条用例失败不影响其它用例
            logger.exception("用例 %s 执行失败", ctx.case.id)
            self._keep_env(ctx)
            # agent 信息照常算（走与正常路径同一个 collect_agent_info）：重建报表时读方也
            # 调它，两边同一个函数才会给出一致的行——随手填 {} 会让「用了没」两处说法不一。
            agents, models = collect_agent_info(ctx.case, ctx.case_dir)
            # 崩在任何一步都可能，收尾时刻就地补上——回执要自包含，否则 runner 会拿
            # 「整个子进程的结束时刻」去回填，于是 API 报一个值、产物里没有值，两边对不上
            # （重建报表只看产物）。
            ctx.ended_at = ctx.ended_at or _now()
            # error 与 reason 同值：与「运行未正常结束」那条路一个口径——「测试结果摘要」
            # 要能直接看出失败原因。留空的话，重建报表会从回执里拿 error（那才是对的），
            # 于是同一件事在本文件里报空、在报表里报原文，两边对不上。
            note = f"{type(e).__name__}: {e}"
            result = self._result(
                ctx,
                session_id=_session_id_of(ctx.sessions.get(UNDER_TEST)),
                passed=None,
                reason=note,
                error=note,
                agents=agents,
                models=models,
            )
        self._write_result(ctx, result)
        return result

    # ---------- 主流程 ----------

    async def _run_async(self, ctx: CaseContext) -> dict[str, Any]:
        """按 a~j 跑完一条用例（细节见模块头的表）。"""
        case = ctx.case
        ctx.case_dir.mkdir(parents=True, exist_ok=True)

        self._snapshot_baseline(ctx)                     # a
        self._apply_precondition(ctx)                    # b
        self._snapshot_case(ctx)                         # c
        ctx.started_at = _now()                          # d

        session_id = await self._run_under_test(ctx)     # e
        ctx.ended_at = _now()                            # f

        self._dump_sessions(ctx)                         # g
        failure = under_test_failure(                    # 运行终态
            read_turn_results(dumped_session_path(ctx.case_dir, UNDER_TEST))
        )
        evidence = self._export_evidence(ctx)            # h
        verdict = (
            await self._run_judge(ctx, evidence) if failure is None else None   # i
        )
        self._run_stats(ctx)                             # j
        # 再落一次 session：judge 的会话在 i 之后才产生（复制循环幂等）
        self._dump_sessions(ctx)

        agents, models = collect_agent_info(case, ctx.case_dir)
        passed, score, reason = conclusion(case, verdict, failure)

        if failure is not None:
            self._keep_env(ctx)                          # 现场：跑完后的环境整份留下
            # error 与 reason 同一句：summary 的「测试结果摘要」要能直接看出失败原因
            return self._result(
                ctx, session_id=session_id, passed=passed, score=score, reason=reason,
                error=reason, agents=agents, models=models,
            )

        if passed is False:
            self._keep_env(ctx)                          # 判不通过也留（结论站的依据就是环境）

        return self._result(
            ctx, session_id=session_id, passed=passed, score=score, reason=reason, error="",
            agents=agents, models=models,
        )

    def _keep_env(self, ctx: CaseContext) -> None:
        """把跑完后的环境整份留进用例目录（`<用例目录>/env/`）。

        **为什么不靠 core 的 `keep_env_on_failure`**：那个判据是 `WorkerOutcome.ok`，
        只覆盖「执行通道失败」（子进程起不来 / 崩了 / 没产出结果）。而「运行未正常结束」
        与「判定不通过」在 `run()` 里就被收成了正常返回，子进程 exit 0——core 看到的是
        **成功**，既不会退役槽位、也不会保留环境。偏偏这两类是排查时最需要现场的。

        此时环境还在（槽位复用前的 `reset_env` 会把它冲掉），所以现场只能在这一层留。
        留的是整份数据根：与 `baseline/` 一对照就是本次改动，还能看见**未被声明为
        evidence 的表**有没有被写脏——那种"写错地方"在 `evidence.json` 里是看不见的。

        尽力而为：拷贝失败只告警，不往上抛——留现场不该把用例的结论也一起弄丢。
        """
        target = ctx.case_dir / ENV_DIR_NAME
        try:
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(ctx.env_root, target)
        except OSError as e:
            logger.warning("保留现场失败：%s（%s: %s）", target, type(e).__name__, e)
            return
        logger.info("保留现场：%s（跑完后的环境副本，与 baseline/ 对照）", target)

    def _result(
        self,
        ctx: CaseContext,
        *,
        session_id: str,
        passed: bool | None,
        reason: str,
        error: str,
        agents: dict[str, bool],
        models: dict[str, str],
        score: int | None = None,
    ) -> dict[str, Any]:
        """组装一条结果（run 级的 `versions` 由 runner 补，子进程不管）。"""
        return {
            "case_id": ctx.case.id,
            "case_type": ctx.case.type,
            "case_dir": str(ctx.case_dir),
            "version": ctx.meta_id,
            "content_summary": content_summary(ctx.case),
            "session_id": session_id,
            "passed": passed,
            "score": score,
            "reason": reason,
            "multi_turn": ctx.case.multi_turn,
            "started_at": ctx.started_at,
            "ended_at": ctx.ended_at,
            "error": error,
            "agents": agents,
            "models": models,
        }

    def _write_result(self, ctx: CaseContext, result: dict[str, Any]) -> None:
        """把**只有内存里才有**的那几个字段落一份到 `case_dir/result.json`。

        为什么只有三个字段：这份回执原来是子进程回传给父进程的载荷，跑完就没了（成功时连
        通信文件都会被清掉）。而 `_result` 的其余字段——结论、session_id、模型名、类型——
        在同目录的产物里**都推得出来**（`case.yaml` / `judge.json` / `session_*.jsonl`）。
        把它们再抄一份进来，等于制造第二个事实来源：两处一旦不一致，读的人无从判断谁对。

        所以这里**只装推不出来的**：

        | 字段 | 为什么别处没有 |
        | --- | --- |
        | `started_at` / `ended_at` | d / f 步在内存里打的点，从没进过 session（session 自己的 `created_at` 是另一回事，实测差 0.87s） |
        | `error` | 用例级异常（agent 工厂抛错、超时）的原文只在内存与子进程日志里 |

        其余字段一律由读方（`summary`）从产物现算，且调用本模块里已有的那些函数——
        保证同一件事只有一份实现。

        尽力而为：写不进去只告警，不往上抛——结论不能因为落盘失败而一起丢掉。
        """
        payload = {
            "started_at": result.get("started_at", ""),
            "ended_at": result.get("ended_at", ""),
            "error": result.get("error", ""),
        }
        target = ctx.case_dir / RESULT_FILENAME
        try:
            ctx.case_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.warning("落 result.json 失败：%s（%s: %s）", target, type(e).__name__, e)

    # ---------- a / b / c ----------

    def _snapshot_baseline(self, ctx: CaseContext) -> None:
        """a. 把环境当下的样子存成基线（取证时对比用）。

        必须在 precondition **之前**：precondition 写进 `custom_prompt.md` 的规则本身
        也是证据（有用例判「规则文件终态」），打在其后就把这一步藏起来了。
        """
        snapshot_baseline(ctx.env_root, ctx.baseline_dir)

    def _apply_precondition(self, ctx: CaseContext) -> None:
        """b. 把 `case.precondition.rules` 写入 `custom_prompt.md`（无规则则不动）。"""
        rules = ctx.case.precondition.rules
        if not rules:
            return
        path = ctx.env_root / "agent" / "chat" / "custom_prompt.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(_upsert_rule_section(text, rules), encoding="utf-8")

    def _snapshot_case(self, ctx: CaseContext) -> None:
        """c. 把该用例的定义快照到 `case_dir/case.yaml`（保证 run 自包含）。"""
        snapshot_case(ctx.case, ctx.case_dir)

    # ---------- e / f：跑被测评 agent ----------

    async def _run_under_test(self, ctx: CaseContext) -> str:
        """e. 运行被测评 agent，返回其 session_id。

        当前只实现 `input_mode=scripted`（按 `turns` 顺序注入）；
        `input_mode=agent`（simulator 驱动）留待后续。

        收尾固定两件事：先 flush 落盘（`persist_session_now`），再 `cancel()` 停掉后台循环。
        """
        if ctx.case.uses_simulator:
            raise NotImplementedError(f"TODO: input_mode=agent 的 simulator 驱动（用例 {ctx.case.id}）")

        agent = self._agent_factory(
            data_path=ctx.env_root,
            session_folder=ctx.session_folder,
            # 注意：不要传 name——lifeprism 复刻 agent 的 SystemPrompt 按固定名注册，
            # 改 name 会导致提示词查不到（提示词为空）。
        )
        ctx.sessions[UNDER_TEST] = agent
        collector = TurnCollector(agent._event_service)
        try:
            await self._drive_scripted(ctx, agent, collector)
        finally:
            agent.persist_session_now()
            agent.cancel()

        return _session_id_of(agent)

    async def _drive_scripted(
        self, ctx: CaseContext, agent: Any, collector: TurnCollector
    ) -> None:
        """按 `turns` 顺序逐轮注入（带 `trigger` 的条件注入尚未实现）。"""
        case = ctx.case
        if not case.turns:
            logger.warning("用例 %s 没有 turns，未执行任何一轮", case.id)
            return
        for index, turn in enumerate(case.turns, start=1):
            if turn.trigger:
                raise NotImplementedError(
                    f"TODO: 带 trigger 的条件注入尚未实现（用例 {case.id}）"
                )
            await self._send_and_wait(ctx, agent, collector, turn.text)
            # 某轮以非 success 收场就停止后续注入：turn 的异常会击穿 loop，
            # 此后 send 会重启已终止的循环，把消息灌进已失败的运行。
            reason = collector.turn_ends[-1] if collector.turn_ends else ""
            if reason and reason != "success":
                logger.warning(
                    "用例 %s 的第 %d 轮以 %s 收场，停止后续轮次注入", case.id, index, reason
                )
                break

    async def _send_and_wait(
        self, ctx: CaseContext, agent: Any, collector: TurnCollector, text: str
    ) -> None:
        """发送一轮用户消息并等待本轮 turn/end。"""
        # TODO: 超时路径与「运行未正常结束」不一致——这里抛错会冒到 run() 的兜底，
        # 该用例只剩 error，i~j（裁判 / 统计）不执行。待统一：超时也应走
        # 「运行未正常结束」收口，照样落证据与统计。
        collector.expect_turn()
        await agent.send(text)
        try:
            await collector.wait_turn(ctx.turn_timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"等待 turn/end 超时（{ctx.turn_timeout}s）") from e

    # ---------- g：session 落盘 ----------

    def _dump_sessions(self, ctx: CaseContext) -> None:
        """g. 把本次用例涉及的各 agent 的 session 复制进 `case_dir`，按语义名重命名。

        session 文件名固定为 `<session_id>.jsonl` 且不支持改名，只能跑完复制改名。
        文件不存在（如 agent 未落盘）时跳过并告警。
        """
        ctx.case_dir.mkdir(parents=True, exist_ok=True)
        for key, agent in ctx.sessions.items():
            session_id = _session_id_of(agent)
            if not session_id:
                logger.warning("会话 %s 取不到 session_id，跳过落盘", key)
                continue
            src = session_file_path(ctx.env_root, ctx.session_folder, session_id)
            if not src.exists():
                logger.warning("会话文件不存在，跳过落盘: %s", src)
                continue
            shutil.copy2(src, dumped_session_path(ctx.case_dir, key))

    # ---------- h：证据 ----------

    def _export_evidence(self, ctx: CaseContext) -> dict:
        """h. 导出 `evidence.json`：与环境基线对比，捞「本次写入」。"""
        evidence = collect_evidence(
            case=ctx.case,
            env_root=ctx.env_root,
            baseline_dir=ctx.baseline_dir,
            db_rel_path=ctx.db_rel_path,
            scan_other_changed_files=ctx.scan_other_changed_files,
        )
        ctx.case_dir.mkdir(parents=True, exist_ok=True)
        (ctx.case_dir / EVIDENCE_FILENAME).write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return evidence

    # ---------- i：裁判 ----------

    async def _run_judge(self, ctx: CaseContext, evidence: dict) -> dict | None:
        """i. `judge.mode=model` 时运行裁判 agent，产出 `judge.json`；`none` 时返回 None。

        输入 = 裁判提示词（agent 内建）+ `case.rubric` + `evidence` + under_test 对话记录
        （从 g 复制出的 `session_under_test.jsonl` 抽取 user/assistant/tool_result）。
        输出 `{"pass": bool, "score": int, "reason": str, ...}`；解析不了则 `pass=None`
        并保留原文（`score` 单独解析，取不到只让它自己为 None，不牵连判定，见 `_parse_verdict`）。
        """
        if not ctx.case.uses_judge:
            return None

        case = ctx.case
        transcript = _read_transcript(dumped_session_path(ctx.case_dir, UNDER_TEST))
        prompt_text = _build_judge_input(case, evidence, transcript)

        agent = self._judge_factory(
            data_path=ctx.env_root,
            session_folder=ctx.session_folder,
        )
        ctx.sessions[JUDGE] = agent
        collector = TurnCollector(agent._event_service)
        try:
            await self._send_and_wait(ctx, agent, collector, prompt_text)
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
        ctx.case_dir.mkdir(parents=True, exist_ok=True)
        (ctx.case_dir / JUDGE_FILENAME).write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return verdict

    # ---------- j：统计 ----------

    def _run_stats(self, ctx: CaseContext) -> dict:
        """j. 对 g 复制出的 `session_under_test.jsonl` 跑通用统计组件，产出 `stats.json`。

        以文件为接口（而非内存里的 agent 对象），使 g~j 可解耦、可单独重跑。
        统计是辅助信息、不参与判定，故 session 缺失时只记进 `errors`，不抛出。
        """
        case_id = ctx.case.id
        session_path = dumped_session_path(ctx.case_dir, UNDER_TEST)
        if not session_path.is_file():
            logger.warning("用例 %s 的 session 文件不存在，跳过统计: %s", case_id, session_path)
            merged: dict = _stats_error(session_path, f"session 文件不存在: {session_path.name}")
        else:
            try:
                merged = analyze_session(session_path)
            except Exception as e:  # noqa: BLE001 - 统计是辅助信息，解析失败只记录，不拖垮用例
                logger.warning("用例 %s 统计失败: %s", case_id, e)
                merged = _stats_error(session_path, f"{type(e).__name__}: {e}")

        ctx.case_dir.mkdir(parents=True, exist_ok=True)
        (ctx.case_dir / STATS_FILENAME).write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return merged


# ---------------- 子进程 entrypoint ----------------


def execute_case(payload: dict, env: Any) -> dict:
    """一条用例的执行入口（`evaluate` 的 `fn(payload, env) -> dict` 契约）。

    这是子进程里真正跑起来的函数（`entrypoint = "lifeprismevalue.evalue.case:execute_case"`），
    它把「子进程」与「领域流程」接上：切数据根 → 读用例 → 交给 `CaseExecutor`。

    payload 的字段（由 runner 组装，见 runner.py）：
        cases_path / index / case_dir / baseline_dir / session_folder /
        turn_timeout / scan_other_changed_files
    """
    # 切数据根必须早于创建任何 agent：工具只经 config.get_db_path() 取库
    config.use_data_path(env.root)

    case_set = load_case_set(payload["cases_path"])
    # 库在哪：读用例文件自己的声明（子进程本来就加载了它）——不猜一个路径，
    # 也不从环境句柄绕（句柄只认数据根，不认识存储细节）
    db = case_set.meta.env.db
    ctx = CaseContext(
        case=case_set.cases[int(payload["index"])],
        meta_id=case_set.meta.id,
        case_dir=Path(payload["case_dir"]),
        env_root=Path(env.root),
        baseline_dir=Path(payload["baseline_dir"]),
        db_rel_path=db.path if db is not None else "",
        session_folder=Path(payload["session_folder"]),
        turn_timeout=float(payload.get("turn_timeout", DEFAULT_TURN_TIMEOUT)),
        scan_other_changed_files=bool(payload.get("scan_other_changed_files", True)),
    )
    return CaseExecutor().run(ctx)


def default_under_test_agent_factory(**kwargs: Any) -> Any:
    """默认的被测评 agent：lifeprism 复刻 agent（延迟导入，避免拖入重依赖）。"""
    from lifeprismevalue.agent.old_lifeprism_agent import create_old_agent

    return create_old_agent(**kwargs)


def default_judge_agent_factory(**kwargs: Any) -> Any:
    """默认的裁判 agent：judge_agent（延迟导入）。"""
    from lifeprismevalue.agent.judge_agent import create_judge_agent

    return create_judge_agent(**kwargs)


# ---------------- 模块级辅助 ----------------


def _session_event_name() -> str:
    """session/event 的事件名（延迟导入 myagent，避免顶层拖入重依赖）。"""
    from myagent.infra.events.eventspec import SESSION_EVENT

    return SESSION_EVENT.name


def _now() -> str:
    """当前 UTC 时间（ISO 8601），用于会话起止时间（归因不靠时间）。"""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def session_file_path(env_root: Path, session_folder: Path, session_id: str) -> Path:
    """按 session_id + 数据根定位 session 文件（与 SessionStore 的规则一致）。"""
    return project_path_to_session_folder(env_root, session_folder) / f"{session_id}.jsonl"


def dumped_session_path(case_dir: Path, role: str) -> Path:
    """用例目录里某个角色 agent 的 session 副本路径（`session_<角色>.jsonl`）。

    写方是 `_dump_sessions`，读方（summary）也从这里取——文件名只有一处定义，免得两处
    各写一份字符串，改一处漏一处时表现是「某一列静默空掉」。
    """
    return case_dir / f"session_{role}.jsonl"


def snapshot_case(case: Case, case_dir: Path) -> Path:
    """把用例定义快照写成 `case_dir/case.yaml`（保证 run 自包含）。

    写方有两处：跑起来的用例由 c 步写；子进程压根没起来时由 runner 补（它手里有同一个
    `Case` 对象）。两处共用这一个函数——同一份快照出现两个写法，迟早会分叉。
    """
    case_dir.mkdir(parents=True, exist_ok=True)
    path = case_dir / CASE_FILENAME
    path.write_text(
        yaml.safe_dump(asdict(case), allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def content_summary(case: Case) -> str:
    """测试内容摘要：用例编号 + 类型（多轮再标注）。"""
    suffix = "（多轮）" if case.multi_turn else ""
    return f"{case.id}·{case.type}{suffix}"


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


def read_turn_results(session_path: Path) -> list[dict]:
    """从 session jsonl 读出每一轮的终态（`turn/end`）。

    这是「本轮跑成什么样」的唯一权威来源：`TURN_END` 事件的 payload 不带信息，而
    `turn/end` 记录（`TurnEndData`）里有本轮 `FinalResult` 的 `reason_type` /
    `reason_text` / `error_type`——`error_type` 是最外层异常的类名（如
    AgentUnclaimedError / RetryExhaustedError / MaxStepsExceededError），`reason_text`
    是异常链文本（逐层 `类型: 消息`）。

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


def under_test_failure(turn_results: list[dict]) -> dict | None:
    """取第一条非 success 的 turn 终态（error / interrupted）；都正常则返回 None。

    只报告、不重试：运行本身没跑完的用例，其结论不成立，不能当成「agent 记错了」计分。
    """
    for item in turn_results:
        if item["reason_type"] and item["reason_type"] != "success":
            return item
    return None


def failure_note(turn: dict) -> str:
    """把 turn 的异常终态整理成一句可读摘要（写进 summary 的「测试结果摘要」）。

    `error_type` 是异常类别（最外层异常类名），`reason_text` 是异常链文本
    （逐层 `类型: 消息`，见 loop 的 `_format_error_chain`），两者互补。
    """
    head = f"运行未正常结束（turn {turn['turn']} · {turn['reason_type']}）"
    detail = "｜".join(part for part in (turn["error_type"], turn["reason_text"]) if part)
    return f"{head}：{_truncate(detail, FAILURE_NOTE_MAX_CHARS)}" if detail else head


def conclusion(
    case: Case | None, verdict: dict | None, failure: dict | None
) -> tuple[bool | None, int | None, str]:
    """一条用例的最终结论：`(是否通过, 得分, 理由)`。

    **跑的时候与重建报表的时候共用这一个函数**（写方在 `_run_async`，读方在 summary）。
    两边各判一次必然漂，而漂的表现是「报表的结论和运行时的结论对不上」，属于最难查的一类
    错——所以宁可让读方多绕一步 import，也不许第二份实现。

    - 运行未正常结束（`failure` 非空）：**不判**，理由用 `failure_note`——运行本身没跑完
      的用例，其结论不成立，不能当成「agent 记错了」计分；
    - 没跑裁判（`judge.mode=none`）：不判，理由写明是配置如此；
    - 判了：取裁判的 `pass` / `score` / 理由（理由缺失时退回解析错误）。

    `case` 为 None（用例快照都没落下来）时无从判断配置，一律按"不判"处理。

    读 `verdict` 一律用 `.get`：跑的时候它是内存里的 dict（键必在），重建的时候它是从
    `judge.json` 读回来的（键可能因版本或手改而缺），两种输入都要接得住。
    """
    if failure is not None:
        return None, None, failure_note(failure)
    if verdict is None:
        mode = case.judge.mode if case is not None else ""
        return None, None, f"judge.mode={mode}，未判定"
    return (
        verdict.get("pass"),
        verdict.get("score"),
        verdict.get("reason") or verdict.get("parse_error") or "",
    )


def _read_transcript(session_path: Path) -> str:
    """从 session jsonl 抽出 user / assistant / tool_result 三类消息，拼成交给裁判的对话文本。

    直接按 jsonl 的字段解析（不依赖 session 的类型定义），避免反向依赖 myagent 内部结构。
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
    """从裁判输出里抽出 `{"pass": bool, "score": int, "reason": str}`。

    容错点（LLM 输出是外部边界）：剥掉 ``` 围栏、只取最外层 JSON、`pass` 允许是
    "true"/"通过" 这类字符串、`score` 允许是字符串数字或浮点。确实解析不出 `pass` 时
    `pass=None`，把原文留给人工看。

    `score` 与 `pass` **互不拖累**：`score` 缺失或非法只让它自己为 None，不写 `parse_error`
    ——那条错误的语义是「整条判定不成立」，而 `pass` 有效时判定是成立的，只是这次没拿到分。
    """
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {
            "pass": None, "score": None, "reason": "",
            "parse_error": "裁判输出中未找到 JSON",
        }
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        return {"pass": None, "score": None, "reason": "", "parse_error": f"JSON 解析失败: {e}"}

    reason = str(data.get("reason", ""))
    score = _parse_score(data.get("score"))
    passed = data.get("pass")
    if isinstance(passed, str):
        passed = passed.strip().lower() in {"true", "yes", "y", "通过", "达标"}
    if not isinstance(passed, bool):
        return {"pass": None, "score": score, "reason": reason, "parse_error": "缺少布尔字段 pass"}
    return {"pass": passed, "score": score, "reason": reason, "parse_error": ""}


def _parse_score(value: Any) -> int | None:
    """把裁判给的 `score` 收成 0~100 的整数；缺失或不是数字则返回 None。

    容错：字符串数字（`"85"`）与浮点（`85.6`）都收；越界夹到 `[0, 100]`——裁判偶尔会写
    原始分（判分要点说「满分 5 分」却给了 4）或负分，夹住比整条丢掉有用。

    两个必须显式挡掉的：`bool`（Python 里 `True` 是 `int` 的实例，不挡就会把 `true` 读成
    1 分）与非有限数（`json.loads` 默认接受 `NaN` / `Infinity` 字面量，`round(nan)` 会抛
    ValueError，而这是外部边界，不该让它冒到用例层）。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return max(0, min(100, round(value)))


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


def collect_agent_info(case: Case, case_dir: Path) -> tuple[dict[str, bool], dict[str, str]]:
    """标出本次用了哪些 agent，并取各自 session 里的模型名。

    三个角色相互独立：under_test 恒存在；simulator 仅 `input_mode=agent`；judge 仅
    `judge.mode=model`。没启用时模型名记 `none`，启用但 session 里取不到记 `unknown`
    ——两者分开，便于区分「没用」与「取不到」。

    Returns:
        (角色 -> 是否启用, 角色 -> 模型名)
    """
    used: dict[str, bool] = {
        UNDER_TEST: True,
        SIMULATOR: case.uses_simulator,
        JUDGE: case.uses_judge,
    }
    models: dict[str, str] = {}
    for role, is_used in used.items():
        if not is_used:
            models[role] = MODEL_NOT_USED
            continue
        models[role] = read_model_name(dumped_session_path(case_dir, role)) or MODEL_UNKNOWN
    return used, models


def read_model_name(session_path: Path) -> str:
    """从 session jsonl 的第一条 `request/header` 里取 model_name；取不到返回空串。

    直接按 jsonl 字段解析（不依赖 session 的类型定义），与 `read_turn_results` 同路数。
    """
    if not session_path.is_file():
        return ""
    for raw_line in session_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "request/header":
            continue
        data = record.get("data") or {}
        return str(data.get("model_name") or "")
    return ""


def read_session_id(session_path: Path) -> str:
    """从 session jsonl 的第一条 `meta_data` 里取 session_id；取不到返回空串。

    跑的时候 session_id 是从 agent 对象上取的（`_session_id_of`），重建报表时 agent 早
    没了，只能从落盘的 session 文件里读回来——与 `read_model_name` 同路数，逐行容错。
    """
    if not session_path.is_file():
        return ""
    for raw_line in session_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "meta_data":
            continue
        return str(record.get("session_id") or "")
    return ""


def _session_id_of(agent: Any) -> str:
    """从 agent 上取 session_id（agent._session.meta_data.session_id）。"""
    session = getattr(agent, "_session", None)
    meta = getattr(session, "meta_data", None)
    return getattr(meta, "session_id", "") or ""
