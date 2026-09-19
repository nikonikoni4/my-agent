"""run 级：任务组装、槽位复用、并行、失败现场保留、summary 落盘。

整条链路走**真子进程**（假 entrypoint，见 fake_case.py），所以这里验的是「runner 装配
EvalCore → 开槽 → 子进程 → 产出映射 → 落盘」这一整套，而不碰 LLM。
"""

from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

import pytest
from evaluate.core import WorkerTask

from lifeprismevalue.evalue import summary
from lifeprismevalue.evalue.caseload import CaseLoadError, load_case_set
from lifeprismevalue.evalue.runner import (
    DEFAULT_CASE_ENTRYPOINT,
    CaseResult,
    EvalRunner,
)
from lifeprismevalue.evalue.summary import SUMMARY_COLUMNS, SUMMARY_FILENAME
from helpers import CASES_AGENT_MODE, fake_cases, make_base, write_cases

FAKE_ENTRYPOINT = "lifeprismevalue.evalue.tests.fake_case:execute"
META_ID = "假用例-01"


def run(tmp_path: Path, body: str, **kwargs):
    """跑一次 run，返回 (results, runs_dir, run_dir)。"""
    base = kwargs.pop("base", None) or make_base(tmp_path)
    runs_dir = tmp_path / "runs"
    runner = EvalRunner(
        base_dir=base,
        runs_dir=runs_dir,
        case_entrypoint=kwargs.pop("case_entrypoint", FAKE_ENTRYPOINT),
        **kwargs,
    )
    results = asyncio.run(runner.run(write_cases(tmp_path, body)))
    (run_dir,) = [path for path in runs_dir.iterdir() if path.is_dir()]
    return results, runs_dir, run_dir


def interval(result: CaseResult) -> tuple[float, float]:
    """读假用例写下的运行区间（起始都在 sleep 前后）。"""
    data = json.loads((Path(result.case_dir) / "events.json").read_text(encoding="utf-8"))
    return data["start"], data["end"]


def overlaps(first: tuple[float, float], second: tuple[float, float]) -> bool:
    return first[0] < second[1] and second[0] < first[1]


# ---------------- 基本流程 ----------------


def test_run_产出与用例同序并写summary(tmp_path) -> None:
    results, runs_dir, run_dir = run(tmp_path, fake_cases("ok-1", "ok-2"), max_workers=2)

    assert [r.case_id for r in results] == ["ok-1", "ok-2"]
    assert [Path(r.case_dir).name for r in results] == ["000_ok-1", "001_ok-2"]
    assert Path(results[0].case_dir).parent.name == META_ID
    assert all(Path(r.case_dir).is_dir() for r in results)

    # 每条用例目录里必有一份回执（重建 summary 靠它认人；不落它用例会从报表里消失）
    assert all((Path(r.case_dir) / "result.json").is_file() for r in results)

    rows = list(csv.DictReader((run_dir / SUMMARY_FILENAME).open(encoding="utf-8")))
    assert len(rows) == 2
    assert list(rows[0].keys()) == SUMMARY_COLUMNS
    assert rows[0]["是否通过"] == "Y"
    assert rows[0]["得分"] == "100"
    assert rows[0]["session_id"] == "fake-0"
    assert rows[0]["类型"] == "假类型"                  # 单独一列，可直接筛选
    assert rows[0]["测试内容摘要"] == "ok-1·假类型"
    assert rows[0]["under_test模型"] == "fake-model"
    assert rows[0]["多轮(Y/N)"] == "N"
    assert rows[0]["耗时(s)"] == "0.0"
    # 起止时间两列已换成「耗时(s)」：绝对时间锚点只剩「时间」列
    assert "会话开始时间" not in rows[0] and "会话结束时间" not in rows[0]

    # 全局总表也追加了同一批
    global_rows = list(csv.DictReader((runs_dir / SUMMARY_FILENAME).open(encoding="utf-8")))
    assert [row["session_id"] for row in global_rows] == ["fake-0", "fake-1"]


def test_run_写run_json(tmp_path) -> None:
    _, _, run_dir = run(tmp_path, fake_cases("ok-1"), max_workers=3)

    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["run_id"] == run_dir.name
    assert meta["cases_id"] == META_ID
    assert meta["dataset_version"] == 1
    assert meta["case_count"] == 1
    assert meta["max_workers"] == 3          # 并发度影响可比性，必须留痕
    assert set(meta["versions"]) == {"prompt", "tools", "react"}
    assert meta["started_at"]
    # 环境配置也留痕：换了初始状态，结果就不能跟旧 run 相提并论
    assert meta["env"]["copy"] == ["agent/", "prompts/"]
    assert meta["env"]["db"] == {
        "path": "dataset/lifewatch_ai.db",
        "mode": "copy",
        "keep_tables": [],
    }


def test_run_建好环境与通信目录(tmp_path) -> None:
    _, _, run_dir = run(tmp_path, fake_cases("ok-1"))

    for name in ("envs", "ipc", "logs", "sessions"):
        assert (run_dir / name).is_dir()


def test_run_成功的任务不留通信文件(tmp_path) -> None:
    _, _, run_dir = run(tmp_path, fake_cases("ok-1", "ok-2"))

    assert list((run_dir / "ipc").iterdir()) == []


def test_run_子进程日志按用例目录名落盘(tmp_path) -> None:
    _, _, run_dir = run(tmp_path, fake_cases("ok-1", "ok-2"))

    for name in ("000_ok-1.log", "001_ok-2.log"):
        assert (run_dir / "logs" / name).is_file()


def test_run_跑完回收所有环境(tmp_path) -> None:
    """默认不倒失败现场时，跑完 envs 里什么都不该剩。"""
    _, _, run_dir = run(tmp_path, fake_cases("ok-1", "ok-2"), max_workers=1)

    assert list((run_dir / "envs").iterdir()) == []


# ---------------- 隔离与并行 ----------------


def test_run_槽位复用前整份回滚(tmp_path) -> None:
    """两条用例共用一个槽位：第二条看到的必须是空环境。

    假用例会把「上一条留下的标记」读出来并写进结果——若复用前没回滚，第二条会读到
    「被用例 1 写过」。这是把「必须重置」这条契约变成可执行断言。
    """
    results, _, _ = run(tmp_path, fake_cases("observe-1", "observe-2"), max_workers=1)

    observed = [
        json.loads((Path(r.case_dir) / "observed.json").read_text(encoding="utf-8"))["observed"]
        for r in results
    ]
    assert observed == ["", ""]


def test_run_多个槽位会真正并行(tmp_path) -> None:
    results, _, _ = run(tmp_path, fake_cases("sleep-1", "sleep-2"), max_workers=2)

    assert overlaps(interval(results[0]), interval(results[1]))


def test_run_单槽位时不会并行(tmp_path) -> None:
    """对照组：并行来自槽位数，不是来自「任务恰好很快」。"""
    results, _, _ = run(tmp_path, fake_cases("sleep-1", "sleep-2"), max_workers=1)

    assert not overlaps(interval(results[0]), interval(results[1]))


# ---------------- 失败 ----------------


def test_run_执行通道失败时记错误并保留现场(tmp_path) -> None:
    results, _, run_dir = run(
        tmp_path, fake_cases("fail-1", "ok-1"), max_workers=1, keep_env_on_failure=True
    )

    assert results[0].passed is None
    assert "故意失败" in results[0].error
    assert results[0].reason == results[0].error     # summary 里要能直接看出原因
    assert results[1].error == ""                    # 单条失败不影响其它用例

    # 失败槽位退役：现场留在 envs/ 下（不销毁），且只有它被留下
    kept = [path.name for path in (run_dir / "envs").iterdir()]
    assert kept == ["env01"]

    rows = list(csv.DictReader((run_dir / "summary.csv").open(encoding="utf-8")))
    assert len(rows) == 2
    assert rows[0]["是否通过"] == ""                 # 未判定：不是 Y 也不是 N
    assert "故意失败" in rows[0]["测试结果摘要"]


def test_run_关掉保留现场则一切照常回收(tmp_path) -> None:
    results, _, run_dir = run(
        tmp_path, fake_cases("fail-1"), max_workers=1, keep_env_on_failure=False
    )

    assert "故意失败" in results[0].error
    assert list((run_dir / "envs").iterdir()) == []


def test_run_base_不存在时报错(tmp_path) -> None:
    runner = EvalRunner(base_dir=tmp_path / "nope", runs_dir=tmp_path / "runs")

    with pytest.raises(FileNotFoundError):
        asyncio.run(runner.run(write_cases(tmp_path, fake_cases("ok-1"))))


# ---------------- 环境声明 ----------------

CASES_NO_ENV = """
meta:
  id: 假用例-02
cases:
  - id: ok-1
    type: 假类型
    evidence: [fake_target]
    turns:
      - {role: user, text: "第 1 条"}
    rubric: 应通过
"""


def test_run_缺环境声明时开跑前就报错(tmp_path) -> None:
    """`meta.env` 必填：不写就要报错，不能让「忘写」退化成「整份复制底座」。"""
    runner = EvalRunner(base_dir=make_base(tmp_path), runs_dir=tmp_path / "runs")

    with pytest.raises(CaseLoadError, match="meta.env"):
        asyncio.run(runner.run(write_cases(tmp_path, CASES_NO_ENV)))


def test_run_声明路径不在底座里时开跑前就报错(tmp_path) -> None:
    """路径写错不该表现成「某条用例莫名失败」，而是整个 run 不启动。"""
    body = fake_cases("ok-1").replace("copy: [agent/, prompts/]", "copy: [没有的目录/]")
    runner = EvalRunner(base_dir=make_base(tmp_path), runs_dir=tmp_path / "runs")

    with pytest.raises(FileNotFoundError, match="没有的目录/"):
        asyncio.run(runner.run(write_cases(tmp_path, body)))

    assert not (tmp_path / "runs").exists()      # 没建 run 目录


def test_run_环境声明漏了提示词时开跑前就报错(tmp_path) -> None:
    """提示词是环境的必需项：漏了它 agent 起不来，整个 run 也不该启动。"""
    body = fake_cases("ok-1").replace("copy: [agent/, prompts/]", "copy: [agent/]")
    runner = EvalRunner(base_dir=make_base(tmp_path), runs_dir=tmp_path / "runs")

    with pytest.raises(ValueError, match="prompts/agent_prompts.yaml"):
        asyncio.run(runner.run(write_cases(tmp_path, body)))


def test_run_证据表写错名字时开跑前就报错(tmp_path) -> None:
    """表名打错不该表现成「agent 没记」：原来只会在判分时显形（该项 0 行）。"""
    body = fake_cases("ok-1").replace(
        "evidence: [custom_expense_log]", "evidence: [custom_expence_log]"
    )
    runner = EvalRunner(base_dir=make_base(tmp_path), runs_dir=tmp_path / "runs")

    with pytest.raises(ValueError, match="custom_expence_log"):
        asyncio.run(runner.run(write_cases(tmp_path, body)))


def test_run_按表取证据却没声明库时开跑前就报错(tmp_path) -> None:
    body = fake_cases("ok-1").replace(
        "    db: {path: dataset/lifewatch_ai.db, mode: copy}\n", ""
    )
    runner = EvalRunner(base_dir=make_base(tmp_path), runs_dir=tmp_path / "runs")

    with pytest.raises(ValueError, match="没声明 db"):
        asyncio.run(runner.run(write_cases(tmp_path, body)))


# ---------------- summary 可重建（summary 与跑流程解耦的验收判据） ----------------


def assert_rebuild_matches(run_dir: Path, results: list[CaseResult]) -> None:
    """**对拍**：runner 手里那份内存结论，与从产物重建出来的行，必须逐格一致。

    这是**跨实现**的一致性检查——「跑的时候的结论」与「报表里的结论」应当来自同一条路
    （都调 `case.py:conclusion`），任一环节漂了这里就会红。

    只验「重建 == 之前写出来的那份」是不够的：那两份都是 summary 自己的输出，两边一起错
    也发现不了。对拍拿的是另一个来源（子进程回传给 runner 的那份 `CaseResult`）。
    """
    rows = summary.build_rows(run_dir)
    assert len(rows) == len(results), "用例数与行数对不上"

    for result, row in zip(results, rows):
        assert row["类型"] == result.case_type
        assert row["测试内容摘要"] == result.content_summary
        assert row["多轮(Y/N)"] == ("Y" if result.multi_turn else "N")
        assert row["session_id"] == result.session_id
        assert row["是否通过"] == (
            "" if result.passed is None else ("Y" if result.passed else "N")
        )
        assert row["得分"] == ("" if result.score is None else str(result.score))
        assert row["测试结果摘要"] == result.reason
        assert row["时间"] == (result.ended_at or result.started_at)
        assert row["耗时(s)"] == summary.duration_seconds(result.started_at, result.ended_at)
        for role in ("under_test", "simulator", "judge"):
            assert row[role] == ("Y" if result.agents.get(role) else "N")
            assert row[f"{role}模型"] == result.models.get(role, "")


def test_删掉summary还能原样重建(tmp_path) -> None:
    """**这是本次改造的验收判据**：删光 summary.csv，重建要一模一样。

    做法：跑一次 run → 抄下 runner 写出来的那份 → 删掉它 → 走**命令行那条路径**
    （`summary.rebuild`，写到旁路的 `summary.rebuild.csv`）→ 逐字节比对。

    不一致就说明还有字段只活在内存里：跑的时候用过、却没落到产物上。改造前「时间 / 耗时」
    正是如此（起止时间只在子进程回传给父进程的载荷里过了一趟，没进任何文件）。
    """
    _, _, run_dir = run(tmp_path, fake_cases("ok-1", "ok-2"))
    summary_path = run_dir / SUMMARY_FILENAME
    expected = summary_path.read_text(encoding="utf-8")

    summary_path.unlink()
    assert not summary_path.exists()

    (rebuilt,) = summary.rebuild(run_dir)

    # 主动重建不碰正式表：它**默认写到旁路文件**，所以这条命令不可能覆盖 runner 的产物
    assert rebuilt == run_dir / summary.REBUILD_FILENAME
    assert rebuilt.read_text(encoding="utf-8") == expected


def test_跑完的那些行与内存结论对拍(tmp_path) -> None:
    """跑一次真链路，把 runner 报的与重建出来的逐格对拍（含用例级失败的那条）。"""
    results, _, run_dir = run(tmp_path, fake_cases("ok-1", "fail-1", "ok-2"), max_workers=1)

    assert_rebuild_matches(run_dir, results)


def test_执行通道失败也留一份回执(tmp_path) -> None:
    """子进程没起来时没人落产物 —— runner 得替它补。

    不补的话这条用例会从报表里**整个消失**，连「它失败过」都看不出来。
    """
    results, _, run_dir = run(tmp_path, fake_cases("fail-1", "ok-1"), max_workers=1)

    case_dir = Path(results[0].case_dir)
    assert (case_dir / "case.yaml").is_file()          # 用例快照也补了，类型等列才有值
    data = json.loads((case_dir / "result.json").read_text(encoding="utf-8"))
    # 回执**只装推不出来的三样**——其余列一律由 summary 从产物现算
    assert set(data) == {"started_at", "ended_at", "error"}
    assert "故意失败" in data["error"]

    row = summary.build_rows(run_dir)[0]
    assert "故意失败" in row["测试结果摘要"]            # 原文来自回执
    assert row["是否通过"] == ""                       # 未判定：空，不是 N
    assert row["类型"] == "假类型"                     # 来自补的用例快照


# ---------------- 入口契约 ----------------


def test_默认_entrypoint_能解析到真执行实体() -> None:
    """默认 entrypoint 指向 case.py 的 execute_case（子进程据此 import 回来）。"""
    assert DEFAULT_CASE_ENTRYPOINT == "lifeprismevalue.evalue.case:execute_case"
    assert callable(WorkerTask.load_entrypoint(DEFAULT_CASE_ENTRYPOINT))


def test_用真执行实体跑一条用例_不调LLM(tmp_path) -> None:
    """真 entrypoint（`case.py:execute_case`）在子进程里被真正拉起并跑通。

    用 `input_mode=agent` 的用例：它在创建 agent 之前就收口（simulator 尚未实现），
    因此不需要 LLM，但「子进程 → 切数据根 → 读用例 → 建上下文 → 用例快照 → 结果回传」
    这条链路整条都被走了一遍。
    """
    results, _, run_dir = run(
        tmp_path,
        CASES_AGENT_MODE,
        max_workers=1,
        case_entrypoint=DEFAULT_CASE_ENTRYPOINT,
    )

    result = results[0]
    assert result.error.startswith("NotImplementedError")
    assert result.passed is None
    case_dir = Path(result.case_dir)
    assert (case_dir / "case.yaml").exists()          # c 步产物（真 entrypoint 写的）
    assert (case_dir / "baseline").is_dir()            # a 步产物：环境初始态快照
    # 失败现场：跑完后的环境副本（跨进程也要留得下来）——NotImplementedError 属
    # 「运行未正常结束」，core 那边看不到，只能由 case.py 自己留
    assert (case_dir / "env").is_dir()
    assert not (case_dir / "judge.json").exists()      # 未判定
    assert (run_dir / "logs" / f"{case_dir.name}.log").is_file()


def test_真执行实体的产物能被summary重建(tmp_path) -> None:
    """端到端：**真** `case.py` 落的产物，喂给 `summary` 能算出一行来，且与内存结论对拍。

    上面那条验的是「case.py 跑得起来」，这条验的是「它落的东西够重建报表」——两条缺一不可：
    产物少落了，run 级测试（走假 entrypoint）是看不出来的，假实体自己会把产物补齐。
    """
    results, _, run_dir = run(
        tmp_path,
        CASES_AGENT_MODE,
        max_workers=1,
        case_entrypoint=DEFAULT_CASE_ENTRYPOINT,
    )
    result = results[0]

    assert (Path(result.case_dir) / "result.json").is_file()
    assert result.error.startswith("NotImplementedError")

    assert_rebuild_matches(run_dir, results)
