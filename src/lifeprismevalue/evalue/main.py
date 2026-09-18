"""评测执行入口：命令行跑一个 cases.yaml（真调 LLM、真复制底座）。

用法（在项目根目录运行；LLM 的 MODEL / BASE_URL / ARK_API_KEY 从根 .env 读）：

    python -m lifeprismevalue.evalue.main                                    # 跑默认大类（记录任务-01）
    python src/lifeprismevalue/evalue/main.py                                # 同上（脚本方式，见下方 sys.path 处理）
    python -m lifeprismevalue.evalue.main lifeprismTestData/defs/记录任务-02/cases.yaml
    python -m lifeprismevalue.evalue.main --max-workers 2

其余参数（底座目录、结果目录、单轮超时、失败现场保留等）都用 `EvalRunner` 的默认值，
需要时改 runner.py 里的默认，不在这里开成开关。

退出码：0 = 全部用例通过；1 = 有用例未通过 / 运行未正常结束；2 = 开跑前就没起来
（用例文件不存在、base 目录不存在、`meta.env` 自检不通过）。

本模块只做两件事：把命令行参数交给 `EvalRunner`，再把结果打印出来。读用例、组任务、
开槽、并行、落盘全在 runner.py（流程见 [README.md](./README.md)）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 项目根（src/lifeprismevalue/evalue/main.py 往上四级），默认路径相对它算，
# 免得「从哪个目录执行」影响跑的是哪份用例、结果落到哪里
REPO_ROOT = Path(__file__).resolve().parents[3]

# 测试根（lifeprismTestData/）：定义区 defs/、底座 base/、结果区 runs/ 都在这棵树下
TEST_DATA_ROOT = REPO_ROOT / "lifeprismTestData"

_SRC_DIR = REPO_ROOT / "src"

# 直接当脚本跑（python src/lifeprismevalue/evalue/main.py）时，sys.path[0] 是本目录
# evalue/，它会把标准库的 `types` 遮蔽掉——当进程里跑没问题（解释器启动时已导入了
# 标准库 types），但 worker.py 会把父进程的 sys.path 透传成子进程的 PYTHONPATH，子进程
# 一启动就在 `from types import ...` 上崩（导到的是 evalue/types.py）。这里把它换成
# src/：既能 import lifeprismevalue，又不遮挡标准库。`-m` 方式启动时无需处理（本块跳过）。
if __package__ in (None, ""):
    _here = os.path.normcase(str(Path(__file__).resolve().parent))
    sys.path[:] = [p for p in sys.path if os.path.normcase(os.path.abspath(p)) != _here]
    if str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))

# 下面这三个 import 必须在上面修正 sys.path 之后：它们要能 import 到 lifeprismevalue
import argparse  # noqa: E402
import asyncio  # noqa: E402
import logging  # noqa: E402

from lifeprismevalue.evalue.caseload import CaseLoadError  # noqa: E402
from lifeprismevalue.evalue.runner import (  # noqa: E402
    DEFAULT_MAX_WORKERS,
    CaseResult,
    EvalRunner,
)

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """只留两个值得每次调的参数：跑哪份用例、开几个槽位。"""
    parser = argparse.ArgumentParser(
        prog="lifeprismevalue.evalue",
        description="按 cases.yaml 跑一次记录任务评测（读用例 → 跑 agent → 取证 → 裁判 → 统计）",
    )
    parser.add_argument(
        "cases",
        nargs="?",
        default=str(TEST_DATA_ROOT / "defs" / "记录任务-01" / "cases.yaml"),
        help="用例文件（cases.yaml）路径，默认跑「记录任务-01」",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help=f"槽位数 = 并行度上限（每槽一份环境 + 一条子进程），默认 {DEFAULT_MAX_WORKERS}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cases_path = Path(args.cases)
    if not cases_path.is_file():
        print(f"用例文件不存在: {cases_path}")
        return 2

    runner = EvalRunner(
        base_dir=TEST_DATA_ROOT / "base",
        runs_dir=TEST_DATA_ROOT / "runs",
        max_workers=args.max_workers,
    )

    try:
        results = asyncio.run(runner.run(cases_path))
    except (CaseLoadError, FileNotFoundError, ValueError) as e:
        # 这三类都是「开跑前就没起来」：用例文件 / 底座 / meta.env 自检，原因直白，不必堆栈
        print(f"run 未启动: {e}")
        return 2

    print_run_report(results)
    return 1 if any(_is_failure(result) for result in results) else 0


# ---------------- 结果打印 ----------------


def _is_failure(result: CaseResult) -> bool:
    """一条用例算不算失败：运行异常，或裁判判了且没通过。"""
    return bool(result.error) or result.passed is False


def _verdict(result: CaseResult) -> str:
    """单条用例的结论列：未判（judge.mode=none 或运行未正常结束）/ 通过 / 未通过。"""
    if result.passed is None:
        return "未判"
    return "通过" if result.passed else "未通过"


def print_run_report(results: list[CaseResult]) -> None:
    """跑完后的收尾报告：逐条结论 + 汇总 + 产物位置。

    排查顺序（见 README「几个必须注意的点」）：先看子进程日志
    `runs/<run_id>/logs/<用例目录名>.log`，再看用例目录里的 `evidence.json` / `judge.json`。
    """
    print(f"\n{'用例':<14}{'结论':<8}摘要")
    for result in results:
        reason = result.error or result.reason
        print(f"{result.case_id:<14}{_verdict(result):<8}{reason}")

    failed = [result for result in results if _is_failure(result)]
    passed_count = sum(1 for result in results if result.passed is True)
    print(f"\n共 {len(results)} 条：通过 {passed_count}，失败 {len(failed)}")

    if results:
        # 用例目录是 runs/<run_id>/<大类>/<NNN>_<用例id>/，往上两级就是本次 run 目录
        run_dir = Path(results[0].case_dir).parents[1]
        print(f"结果: {run_dir / 'summary.csv'}")
        print(f"日志: {run_dir / 'logs'}（子进程日志，一条用例一个文件）")


if __name__ == "__main__":
    raise SystemExit(main())
