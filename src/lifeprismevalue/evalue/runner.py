"""评测执行入口（runner）：装配槽位池，把用例铺上去跑。

本模块只做三件事：

1. **准备 run**：建 `runs/<run_id>/`（`envs/` 环境、`ipc/` 子进程通信、`logs/` 子进程日志、
   `sessions/` 会话落盘）并写 `run.json`（溯源用的三个版本轴 + 起止）；
2. **组任务**：把用例列表翻译成 `WorkerTask` 列表（entrypoint 指向 `case.py` 的执行实体），
   交给 `evaluate.core.EvalCore` 铺到槽位上跑；
3. **收结果**：把 `EvalCore` 交回的产出映射成 `CaseResult`，并让 `summary` 模块
   从**落盘的文件**重建 `summary.csv`。

第 3 步为什么不直接用手里这份 `CaseResult` 拼表：那份数据一旦只活在内存里，
`summary.csv` 就成了唯一副本——删掉它，「时间 / 耗时」这类字段再也算不出来。
所以列的定义搬去了 [summary.py](./summary.py)，这里只负责"跑完调一次"，且走的是与
命令行重建**完全相同**的代码路径（跑评测本身就在检验重建还成不成立）。

用例到底怎么跑、跑成什么样，本模块**不解释**——那是 `case.py`（领域层）与
`WorkerOutcome`（回执）的事。流程与约定见 [README.md](./README.md)。

并行与隔离（由 `EvalCore` + 本模块的 provider 提供，见 env.py / worker.py）：
`max_workers` 个槽位 = 一份环境 + 一条子进程；用例按槽位循环铺开，复用前整份回滚；
失败的任务当场退役、现场留在 `envs/<key>/` 供排查。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evaluate.core import EnvSpec, EvalCore, WorkerOutcome, WorkerTask

from lifeprismevalue.evalue import summary
from lifeprismevalue.evalue.caseload import load_case_set
from lifeprismevalue.evalue.case import (
    DEFAULT_TURN_TIMEOUT,
    collect_agent_info,
    content_summary,
    snapshot_case,
)
from lifeprismevalue.evalue.env import LifeprismEnvProvider, check_env_inputs
from lifeprismevalue.evalue.evidence import is_file_target
from lifeprismevalue.evalue.sqlite_read import table_names
from lifeprismevalue.evalue.types import (
    RESULT_FILENAME,
    RUN_FILENAME,
    Case,
    CaseSet,
    EnvConfig,
)
from lifeprismevalue.versions import (
    AXIS_PROMPT,
    AXIS_REACT,
    AXIS_TOOLS,
    VersionBook,
)

logger = logging.getLogger(__name__)

# 默认的用例执行入口（子进程里 import 回来的 `module.path:function`）
DEFAULT_CASE_ENTRYPOINT = "lifeprismevalue.evalue.case:execute_case"

# 默认槽位数。任务若依赖外部服务（LLM），并发过高只会撞限流；建议不超过物理核数。
DEFAULT_MAX_WORKERS = 4

# run 目录下的子目录：环境（每槽一份）/ 子进程通信 / 子进程日志 / 会话落盘根
ENVS_DIR_NAME = "envs"
IPC_DIR_NAME = "ipc"
LOGS_DIR_NAME = "logs"
SESSIONS_DIR_NAME = "sessions"

# 用例目录下存放「环境初始态快照」的子目录名（取证时与它对比）
BASELINE_DIR_NAME = "baseline"


# ---------------- 运行时数据结构 ----------------


@dataclass
class RunContext:
    """一次 run 的路径与元信息（run 级、跨用例共享）。"""

    run_id: str
    cases_path: Path
    base_dir: Path          # 环境材料的来源（= EnvSpec.template）：环境按 meta.env 从它拼
    runs_dir: Path          # runs/
    run_dir: Path           # runs/<run_id>/
    envs_dir: Path          # runs/<run_id>/envs/：每槽一个环境目录
    ipc_dir: Path           # runs/<run_id>/ipc/：子进程请求 / 结果文件
    log_dir: Path           # runs/<run_id>/logs/：子进程 stdout/stderr
    session_folder: Path    # runs/<run_id>/sessions/：本次 run 的 session 落盘根
    env_config: EnvConfig   # 本次 run 的环境配置（cases.yaml 的 meta.env）
    versions: dict[str, Any] = field(default_factory=dict)  # 三个版本轴的快照（VersionBook）


@dataclass
class CaseResult:
    """单条用例的执行结果（汇总到 summary 用）。"""

    case_id: str
    case_type: str
    case_dir: str
    version: str = ""           # 大类 id（meta.id）
    content_summary: str = ""   # 测试内容摘要
    session_id: str = ""        # 被测评 agent 的 session id
    passed: bool | None = None  # None = 未判（judge.mode=none 或运行未正常结束）
    score: int | None = None    # 裁判给的 0~100 分；None = 未判 / 裁判没给（两种都留空）
    reason: str = ""
    multi_turn: bool = False
    started_at: str = ""
    ended_at: str = ""
    error: str = ""             # 用例级异常 / 执行通道异常（都不中断整个 run）
    versions: dict[str, str] = field(default_factory=dict)  # 版本轴 -> 版本名（run 级）
    agents: dict[str, bool] = field(default_factory=dict)   # agent 角色 -> 本次是否使用
    models: dict[str, str] = field(default_factory=dict)    # agent 角色 -> 模型名


# ---------------- 主类 ----------------


class EvalRunner:
    """按用例执行评测：组任务 → 交给 `EvalCore` 跑 → 收结果写 summary。"""

    def __init__(
        self,
        *,
        base_dir: str | Path,
        runs_dir: str | Path,
        case_entrypoint: str = DEFAULT_CASE_ENTRYPOINT,
        max_workers: int = DEFAULT_MAX_WORKERS,
        turn_timeout: float = DEFAULT_TURN_TIMEOUT,
        scan_other_changed_files: bool = True,
        keep_env_on_failure: bool = True,
    ) -> None:
        """
        Args:
            base_dir: 环境材料的来源（`EnvSpec.template`）：环境按 cases.yaml 的
                `meta.env` 从它拼出来（复制哪些路径、库怎么来，都由那份配置决定）。
            runs_dir: 结果根目录；每次 run 在其下新建 `<run_id>/`。
            case_entrypoint: 用例执行入口（`"module.path:function"`），默认
                `case.py:execute_case`；测试可指向假实现，不必真调 LLM。
            max_workers: 槽位数 = 并行度上限（每个槽位占一份环境 + 一条子进程）。
            turn_timeout: 单轮等待 turn/end 的超时秒数。
            scan_other_changed_files: 取证时是否额外全树对比，把「未被 evidence 声明但
                确实改了」的文本文件也收进 evidence（防漏判）。
            keep_env_on_failure: 执行通道失败的任务是否保留现场（不销毁环境）。
        """
        self.base_dir = Path(base_dir)
        self.runs_dir = Path(runs_dir)
        self.case_entrypoint = case_entrypoint
        self.max_workers = max_workers
        self.turn_timeout = turn_timeout
        self.scan_other_changed_files = scan_other_changed_files
        self.keep_env_on_failure = keep_env_on_failure

    # ---------- 顶层 ----------

    async def run(self, cases_path: str | Path) -> list[CaseResult]:
        """加载用例、铺到槽位上跑完，返回与用例同序的结果。

        单条用例失败（用例级异常、执行通道崩溃）都不中断整个 run：前者由 `case.py` 收进
        结果里，后者由 `EvalCore` 收进 `WorkerOutcome.error`，这里再落成一条 `CaseResult`。
        """
        case_set = load_case_set(cases_path)
        ctx = self._prepare_run(Path(cases_path), case_set)

        tasks = [
            self._build_task(ctx, case_set, index, case)
            for index, case in enumerate(case_set.cases)
        ]
        # 调度是同步的（线程池 + 子进程），扔到线程里跑，免得堵住事件循环
        outcomes = await asyncio.to_thread(self._dispatch, ctx, tasks)

        results = [
            self._to_result(ctx, case_set, index, case, outcome)
            for index, (case, outcome) in enumerate(zip(case_set.cases, outcomes))
        ]
        self._write_summary(ctx)
        return results

    # ---------- 调度 ----------

    def _dispatch(self, ctx: RunContext, tasks: list[WorkerTask]) -> list[WorkerOutcome]:
        """把任务交给 `EvalCore`：开槽 → 派发 → 回收，产出与入参同序。"""
        provider = LifeprismEnvProvider(
            envs_root=ctx.envs_dir,
            ipc_dir=ctx.ipc_dir,
            env_config=ctx.env_config,   # 「怎么造」是本机制的构造参数，不进 EnvSpec
            log_dir=ctx.log_dir,
        )
        core = EvalCore(
            provider,
            EnvSpec(template=ctx.base_dir, label=ctx.run_id),
            max_workers=self.max_workers,
            keep_env_on_failure=self.keep_env_on_failure,
        )
        return core.run(tasks)

    def _build_task(
        self, ctx: RunContext, case_set: CaseSet, index: int, case: Case
    ) -> WorkerTask:
        """把一条用例翻译成一份不透明任务（内容只有 `case.py` 认识）。"""
        return WorkerTask(
            entrypoint=self.case_entrypoint,
            payload={
                "cases_path": str(ctx.cases_path),
                "index": index,
                "case_dir": str(self._case_dir(ctx, case_set, index, case)),
                "baseline_dir": str(self._baseline_dir(ctx, case_set, index, case)),
                "session_folder": str(ctx.session_folder),
                "turn_timeout": self.turn_timeout,
                "scan_other_changed_files": self.scan_other_changed_files,
            },
        )

    def _to_result(
        self,
        ctx: RunContext,
        case_set: CaseSet,
        index: int,
        case: Case,
        outcome: WorkerOutcome,
    ) -> CaseResult:
        """把产出映射成 `CaseResult`（run 级的 `versions` 在这里补上）。

        执行通道失败时（子进程起不来、崩了、没产出结果），产出一份只剩错误信息的
        `CaseResult`——整次 run 仍然完整，失败现场留在 `envs/<key>/`。
        """
        versions = _version_names(ctx.versions)
        data = outcome.output or {}

        if not outcome.ok:
            logger.error(
                "用例 %s 的执行通道失败：%s（环境 %s）", case.id, outcome.error, outcome.env_key
            )
            case_dir = self._case_dir(ctx, case_set, index, case)
            result = CaseResult(
                case_id=case.id,
                case_type=case.type,
                case_dir=str(case_dir),
                version=case_set.meta.id,
                content_summary=content_summary(case),
                multi_turn=case.multi_turn,
                started_at=outcome.started_at,
                ended_at=outcome.ended_at,
                # reason 与 error 都放同一句：summary 的「测试结果摘要」要能直接看出失败原因
                reason=outcome.error,
                error=outcome.error,
                versions=versions,
            )
            _write_failure_artifacts(case_dir, case, result)
            # agent 信息也按**同一个** collect_agent_info 算：子进程没起来，session 一份都
            # 没有，于是模型名是 unknown（「启用但取不到」）而不是留空。两边同一个函数，
            # 重建出来的行才与这里报的一致（对拍测试钉着这条）。
            result.agents, result.models = collect_agent_info(case, case_dir)
            return result

        return CaseResult(
            case_id=str(data.get("case_id") or case.id),
            case_type=str(data.get("case_type") or case.type),
            case_dir=str(data.get("case_dir") or self._case_dir(ctx, case_set, index, case)),
            version=str(data.get("version") or case_set.meta.id),
            content_summary=str(data.get("content_summary") or content_summary(case)),
            session_id=str(data.get("session_id") or ""),
            passed=data.get("passed"),
            score=data.get("score"),
            reason=str(data.get("reason") or ""),
            multi_turn=bool(data.get("multi_turn", case.multi_turn)),
            started_at=str(data.get("started_at") or outcome.started_at),
            ended_at=str(data.get("ended_at") or outcome.ended_at),
            error=str(data.get("error") or ""),
            versions=versions,
            agents=dict(data.get("agents") or {}),
            models=dict(data.get("models") or {}),
        )

    # ---------- 辅助 ----------

    def _prepare_run(self, cases_path: Path, case_set: CaseSet) -> RunContext:
        """建 `runs/<run_id>/` 骨架、核一遍 `meta.env` 并写 `run.json`。

        这里**不复制底座**：底座是只读材料来源，环境由 provider 按环境配置、按槽位拼
        （见 env.py）。也不切 `config` 的数据根——本进程不碰数据根，切根发生在子进程里
        （`case.execute_case` 的第一件事，见那边的说明）。
        """
        if not self.base_dir.is_dir():
            raise FileNotFoundError(f"base 目录不存在: {self.base_dir}")

        env_config = case_set.meta.env
        # 开槽前先自检：配置里的路径 / 库 / 表在底座里都得在。放在这里而不是等建环境，
        # 是为了「整个 run 不启动、原因直白」，而不是留下几条「某条任务失败」
        check_env_inputs(self.base_dir, env_config)
        check_evidence_targets(case_set, env_config, base_dir=self.base_dir)

        run_id = self._make_run_id()
        run_dir = self.runs_dir / run_id
        ctx = RunContext(
            run_id=run_id,
            cases_path=cases_path,
            base_dir=self.base_dir,
            runs_dir=self.runs_dir,
            run_dir=run_dir,
            envs_dir=run_dir / ENVS_DIR_NAME,
            ipc_dir=run_dir / IPC_DIR_NAME,
            log_dir=run_dir / LOGS_DIR_NAME,
            session_folder=run_dir / SESSIONS_DIR_NAME,
            env_config=env_config,
        )
        for path in (ctx.envs_dir, ctx.ipc_dir, ctx.log_dir, ctx.session_folder):
            path.mkdir(parents=True, exist_ok=True)

        # 三个版本轴：提示词取版本库的 active_version；工具 / agent 用仓库 HEAD 反查登记表
        ctx.versions = VersionBook.for_data_path(ctx.base_dir).snapshot()
        _log_versions(ctx.versions)

        run_meta = {
            "run_id": run_id,
            "started_at": self._now(),
            "cases_path": str(cases_path),
            "cases_id": case_set.meta.id,
            "dataset_version": case_set.meta.dataset_version,
            "base_dir": str(self.base_dir),
            "case_count": len(case_set.cases),
            # 并行度是结果可比性的一部分（并发高会撞限流），一并记下来
            "max_workers": self.max_workers,
            # 环境配置同样是结果可比性的一部分：换了初始状态，结果就不能与旧 run 比
            "env": asdict(env_config),
            # 三个版本轴（prompt / tools / react）：提示词取版本库，代码用 rev 反查
            "versions": ctx.versions,
        }
        (ctx.run_dir / RUN_FILENAME).write_text(
            json.dumps(run_meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("run %s 就绪：%d 条用例，%d 个槽位", run_id, len(case_set.cases), self.max_workers)
        return ctx

    def _case_dir(self, ctx: RunContext, case_set: CaseSet, index: int, case: Case) -> Path:
        """用例目录：`runs/<run_id>/<meta.id>/<index:03d>_<case.id>/`。"""
        return ctx.run_dir / case_set.meta.id / f"{index:03d}_{case.id}"

    def _baseline_dir(
        self, ctx: RunContext, case_set: CaseSet, index: int, case: Case
    ) -> Path:
        """基线目录：`<用例目录>/baseline/`（环境初始态快照，取证时与它对比）。"""
        return self._case_dir(ctx, case_set, index, case) / BASELINE_DIR_NAME

    def _write_summary(self, ctx: RunContext) -> None:
        """重建本次 run 的 `summary.csv`，并追加到全局总表。

        输入是**落盘的文件**，不是手里那份 `results`——走同一条路，才能保证「删掉
        summary 还能重建」这件事一直成立（列的定义与读法都在 [summary.py](./summary.py)）。
        """
        summary.write_run_and_append(ctx.run_dir, ctx.runs_dir)

    @staticmethod
    def _now() -> str:
        """当前 UTC 时间（ISO 8601），用于会话起止时间（归因不靠时间）。"""
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    @staticmethod
    def _make_run_id() -> str:
        """run_id：UTC 时间戳 `YYYYmmdd-HHMMSS`。"""
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


# ---------------- 开跑前的自检 ----------------


def check_evidence_targets(case_set: CaseSet, config: EnvConfig, *, base_dir: Path) -> None:
    """核一遍用例声明的取证目标与环境是否自洽（开跑前，而不是等判分时才发现）。

    只核**表类**目标：它们必然落在环境声明的那个库里，所以「按表取证却没声明库」和
    「表名打错」都是必然读不到的证据——但处理方式原来是**照常跑完、判分时才显形**
    （裁判看到「该项 0 行」，于是把「配置打错字」记成「agent 没记」）。放在这里拦。

    文件类目标不核：`diary/<date>.md` 这类本来就该是本次新建的文件，环境里没有是对的。
    """
    tables = sorted(
        {raw for case in case_set.cases for raw in case.evidence if not is_file_target(raw)}
    )
    if not tables:
        return
    if config.db is None:
        raise ValueError(f"用例按表取证据，但 meta.env 没声明 db：{tables}")
    known = table_names(base_dir / config.db.path)
    missing = [table for table in tables if table not in known]
    if missing:
        raise ValueError(f"用例声明的证据表在底座里不存在: {missing}（底座库 {config.db.path}）")


# ---------------- 给执行通道失败的用例补回执 ----------------


def _write_failure_artifacts(case_dir: Path, case: Case, result: CaseResult) -> None:
    """给「执行通道失败」的用例补上产物。

    子进程压根没起来，什么都没落。而重建报表正是靠这些文件认出这条用例，所以这里替它补：

    - `case.yaml`：用例定义快照——与 c 步走**同一个** `snapshot_case`，内容一模一样；
    - `result.json`：回执里只有**推不出来的三样**（见 `case.py:_write_result`）。

    不补的话这条用例会从报表里**整个消失**，连「它失败过」都看不出来。

    ⚠️ 回执里的 `started_at` / `ended_at` 与正常路径**语义不同**：正常路径量的是被测评
    agent 的运行时长（`case.py` 在跑之前 / 跑完之后打点），这里量的是**整个子进程**（由
    `evaluate.core` 打点）——子进程没跑起来，没有更细的点可打。所以这类用例的「耗时(s)」
    会偏大。量具坏了的时候看得见，好过看不见。

    尽力而为：写不进去只告警，不往上抛。
    """
    payload = {
        "started_at": result.started_at,
        "ended_at": result.ended_at,
        "error": result.error,
    }
    target = case_dir / RESULT_FILENAME
    try:
        snapshot_case(case, case_dir)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError) as e:
        logger.warning("补产物失败：%s（%s: %s）", target, type(e).__name__, e)


# ---------------- 版本信息 ----------------


def _version_names(versions: dict[str, Any]) -> dict[str, str]:
    """把 VersionBook.snapshot() 压成 {版本轴: 版本名}（写进 summary 用）。"""
    names: dict[str, str] = {}
    for axis, value in (versions or {}).items():
        if isinstance(value, dict):
            names[axis] = str(value.get("version", ""))
        else:
            names[axis] = str(value)
    return names


def _log_versions(versions: dict[str, Any]) -> None:
    """把本次 run 的三个版本显式打进日志；不可信的项另给 warning。

    「不可信」有两类，都在这里暴露：代码版本轴的工作区有未提交改动（HEAD 不代表实际
    代码），或版本压根没确认出来（版本库缺失 / HEAD 未登记）。
    """
    names = _version_names(versions)
    logger.info(
        "本次 run 版本：提示词=%s 工具=%s agent=%s",
        names.get(AXIS_PROMPT) or "（未知）",
        names.get(AXIS_TOOLS) or "（未知）",
        names.get(AXIS_REACT) or "（未知）",
    )
    for axis in (AXIS_TOOLS, AXIS_REACT):
        info = (versions or {}).get(axis)
        if not isinstance(info, dict):
            continue
        if info.get("error"):
            logger.warning("版本轴 %s 无法确认版本：%s", axis, info["error"])
        if info.get("dirty"):
            logger.warning(
                "版本轴 %s 有未提交改动，HEAD 不代表实际代码：%s", axis, info.get("dirty_files")
            )


# ---------------- 便捷入口 ----------------


async def run_cases(
    cases_path: str | Path,
    *,
    base_dir: str | Path,
    runs_dir: str | Path,
    **kwargs: Any,
) -> list[CaseResult]:
    """一次性执行一个 cases.yaml，返回各用例结果。"""
    return await EvalRunner(base_dir=base_dir, runs_dir=runs_dir, **kwargs).run(cases_path)
