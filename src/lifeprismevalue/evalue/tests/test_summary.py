"""summary 模块：从产物重建报表。

这里**不跑评测**——测试自己在地上摆一个 run 目录（`run.json` + 各用例的 `case.yaml` /
`result.json` / `judge.json` / `session_*.jsonl`），然后看算出来的表对不对。验五件事：

1. **列的口径**：每列取哪份产物的哪个字段、缺数据怎么留空；
2. **派生与写方同源**：结论、agent 标志、模型名都是调 `case.py` 的函数算的，
   所以「跑的时候报什么」与「重建出来是什么」必须一致（`test_runner` 里还有一条
   拿真子进程对拍）；
3. **降级不产生错值**：老 run 没有 `result.json` 时，**只有时间 / 耗时两列**该空，
   其余列照样对——这条是「回执只装推不出来的字段」这个决定的验收判据；
4. **列的声明**：拼错输入名要在开跑前被拦下（不然那一列会静默全空）；
5. **两种跑法不互相覆盖**：自动写 `summary.csv`，主动写 `summary.rebuild.csv`。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import yaml

from lifeprismevalue.evalue import summary
from lifeprismevalue.evalue.summary import (
    REBUILD_FILENAME,
    SUMMARY_COLUMNS,
    SUMMARY_FILENAME,
    Column,
    duration_seconds,
)
from lifeprismevalue.versions import AXIS_PROMPT, AXIS_REACT, AXIS_TOOLS

META_ID = "假大类"
T0 = "2026-01-01T00:00:00+00:00"
T10 = "2026-01-01T00:00:10+00:00"
VERSIONS = {
    AXIS_PROMPT: {"version": "v9"},
    AXIS_TOOLS: {"version": "工具-1"},
    AXIS_REACT: {"version": "agent-1"},
}
# 各角色的模型名：真跑时取自各 session 的 request/header，这里固定下来便于断言
MODELS = {"under_test": "m-under", "judge": "m-judge", "simulator": "m-sim"}


# ---------------- 造产物 ----------------


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _write_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def case_snapshot(
    case_id: str,
    case_type: str = "支出记录",
    *,
    input_mode: str = "scripted",
    judge_mode: str = "model",
    multi_turn: bool = False,
) -> dict:
    """`case.yaml` 的内容（= `asdict(Case)` 的形状）。"""
    return {
        "id": case_id,
        "type": case_type,
        "turns": [{"text": "记一笔", "role": "user", "trigger": None}],
        "evidence": ["custom_expense_log"],
        "rubric": "应记录",
        "precondition": {"rules": [], "fixture": {}},
        "multi_turn": multi_turn,
        "input_mode": input_mode,
        "simulator": None,
        "judge": {"mode": judge_mode},
    }


def write_session(
    case_dir: Path, role: str, session_id: str, *, turn_end: str = "success"
) -> None:
    """落一份最小 session：重建报表分别从这三条记录里取 session_id / 模型名 / 运行终态。"""
    failed = turn_end != "success"
    lines = [
        {"type": "meta_data", "session_id": session_id},
        {"type": "request/header", "data": {"model_name": MODELS[role]}},
        {
            "type": "turn/end",
            "turn": 1,
            "data": {
                "reason_type": turn_end,
                "reason_text": "炸了" if failed else "",
                "error_type": "Boom" if failed else "",
            },
        },
    ]
    path = case_dir / f"session_{role}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8"
    )


def add_case(
    run_dir: Path,
    index: int,
    case_id: str,
    *,
    case_type: str = "支出记录",
    input_mode: str = "scripted",
    judge_mode: str = "model",
    multi_turn: bool = False,
    judged: bool = True,
    passed: bool | None = True,
    score: int | None = 100,
    reason: str = "通过",
    receipt: tuple[str, str, str] | None = (T0, T10, ""),
    turn_end: str = "success",
    with_case_snapshot: bool = True,
) -> Path:
    """往 run 目录里放一条用例目录，并落齐它的产物。

    默认是「判过、通过、有回执」的完整用例。几个开关用来造降级形态：
    - `receipt=None` → 没有 `result.json`（**老 run / 半成品**）
    - `judged=False` → 没有 `judge.json`（未判定）
    - `turn_end="error"` → 运行未正常结束
    - `with_case_snapshot=False` → 连用例快照都没有（产物全丢）
    """
    case_dir = run_dir / META_ID / f"{index:03d}_{case_id}"
    case_dir.mkdir(parents=True, exist_ok=True)

    if with_case_snapshot:
        _write_yaml(
            case_dir / "case.yaml",
            case_snapshot(
                case_id,
                case_type,
                input_mode=input_mode,
                judge_mode=judge_mode,
                multi_turn=multi_turn,
            ),
        )

    write_session(case_dir, "under_test", f"s-{case_id}", turn_end=turn_end)
    if input_mode == "agent":
        write_session(case_dir, "simulator", f"s-{case_id}-sim")
    if judge_mode == "model":
        write_session(case_dir, "judge", f"s-{case_id}-judge")

    if judged:
        _write_json(
            case_dir / "judge.json",
            {"pass": passed, "score": score, "reason": reason, "parse_error": ""},
        )
    if receipt is not None:
        started, ended, error = receipt
        _write_json(
            case_dir / "result.json",
            {"started_at": started, "ended_at": ended, "error": error},
        )
    return case_dir


def make_run(tmp_path: Path, run_id: str = "20260101-000000") -> Path:
    """一个含两条用例的 run 目录。

    - `A-1`：完整（有回执、判过、通过）
    - `A-2`：**老 run 形态**——产物都在，**就是没有 `result.json`**
    """
    run_dir = tmp_path / "runs" / run_id
    _write_json(
        run_dir / "run.json", {"run_id": run_id, "cases_id": META_ID, "versions": VERSIONS}
    )
    add_case(run_dir, 0, "A-1")
    add_case(run_dir, 1, "A-2", case_type="梦境记录", receipt=None, score=90)
    return run_dir


# ---------------- 列的口径 ----------------


def test_重建出的行覆盖全部列(tmp_path) -> None:
    rows = summary.build_rows(make_run(tmp_path))

    assert list(rows[0].keys()) == SUMMARY_COLUMNS
    assert rows[0] == {
        "时间": T10,                       # 绝对时间锚点 = 会话结束时刻
        "提示词版本": "v9",
        "工具版本": "工具-1",
        "agent版本": "agent-1",
        "版本": META_ID,                   # run 级，取自 run.json
        "under_test": "Y",
        "simulator": "N",                  # scripted 用例没用模拟用户
        "judge": "Y",
        "under_test模型": "m-under",
        "simulator模型": "none",           # 没用该 agent
        "judge模型": "m-judge",
        "session_id": "s-A-1",
        "类型": "支出记录",
        "测试内容摘要": "A-1·支出记录",
        "测试结果摘要": "通过",
        "是否通过": "Y",
        "得分": "100",
        "多轮(Y/N)": "N",
        "耗时(s)": "10.0",
    }


def test_选列后按声明表定序(tmp_path) -> None:
    rows = summary.build_rows(
        make_run(tmp_path), columns=summary.select_columns(["得分", "类型", "版本"])
    )

    assert rows[0] == {"类型": "支出记录", "得分": "100", "版本": META_ID}


def test_老run形态_只有时间与耗时为空(tmp_path) -> None:
    """**「回执只装推不出来的字段」这个决定的验收判据。**

    老 run 没有 `result.json`（那是后加的），但 `case.yaml` / `judge.json` /
    `session_*.jsonl` 都在，所以除「时间 / 耗时」外**每一列都该是对的**——
    包括`是否通过`、`得分`、`session_id`、模型名。回执要是把结论也抄了一份，
    这里就会整列空掉、甚至连 `under_test`/`judge` 都塌成 N。
    """
    rows = summary.build_rows(make_run(tmp_path))
    old = rows[1]

    assert old["得分"] == "90"              # 结论从 judge.json 来，不依赖回执
    assert old["是否通过"] == "Y"
    assert old["session_id"] == "s-A-2"
    assert old["under_test模型"] == "m-under"
    assert old["judge模型"] == "m-judge"
    assert old["类型"] == "梦境记录"
    assert old["under_test"] == "Y"          # 不是 N：用了就是用了
    assert old["judge"] == "Y"
    assert old["耗时(s)"] == ""              # 这两列才是真没有出处
    assert old["时间"] == ""


def test_运行未正常结束_不判且摘要给出原因(tmp_path) -> None:
    run_dir = make_run(tmp_path)
    add_case(
        run_dir, 2, "A-3", turn_end="error", judged=False, receipt=None
    )

    row = summary.build_rows(run_dir)[2]

    assert row["是否通过"] == ""             # 空与 N 不是一回事
    assert row["得分"] == ""
    assert "运行未正常结束" in row["测试结果摘要"]
    assert "Boom" in row["测试结果摘要"]     # 异常类别来自 turn/end


def test_未跑裁判时摘要写明是配置如此(tmp_path) -> None:
    run_dir = make_run(tmp_path)
    add_case(run_dir, 2, "A-3", judge_mode="none", judged=False)

    row = summary.build_rows(run_dir)[2]

    assert row["是否通过"] == ""
    assert row["测试结果摘要"] == "judge.mode=none，未判定"
    assert row["judge"] == "N"


def test_用例级异常的原文优先于推导(tmp_path) -> None:
    """agent 工厂抛错 / 超时：那条 error 只活在回执里，推导只能得出「未判定」。

    不优先取它，就会把「环境炸了」说成「没判」——正是最该看见原因的时候把原因丢了。
    """
    run_dir = make_run(tmp_path)
    add_case(
        run_dir, 2, "A-3", judged=False, receipt=(T0, T0, "RuntimeError: 工厂挂了")
    )

    row = summary.build_rows(run_dir)[2]

    assert row["测试结果摘要"] == "RuntimeError: 工厂挂了"
    assert row["是否通过"] == ""


def test_产物全丢时整行留空但不抛(tmp_path) -> None:
    """半成品目录（子进程崩在写产物之前）—— 缺就留空，不能让整次重建崩掉。"""
    run_dir = make_run(tmp_path)
    add_case(
        run_dir, 2, "A-3", with_case_snapshot=False, judged=False, receipt=None
    )
    # 会话文件也没了才算"全丢"
    for role in ("under_test",):
        (run_dir / META_ID / "002_A-3" / f"session_{role}.jsonl").unlink()

    row = summary.build_rows(run_dir)[2]

    assert row["类型"] == ""
    assert row["session_id"] == ""
    assert row["是否通过"] == ""
    assert row["under_test"] == ""           # 不是 N


def test_回执读不动时按无回执处理(tmp_path) -> None:
    run_dir = make_run(tmp_path)
    (run_dir / META_ID / "000_A-1" / "result.json").write_text("{ 坏的", encoding="utf-8")

    row = summary.build_rows(run_dir)[0]

    assert row["耗时(s)"] == ""
    assert row["得分"] == "100"               # 其余列不受影响


@pytest.mark.parametrize(
    "started,ended,expected",
    [
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:10+00:00", "10.0"),
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00.50+00:00", "0.5"),
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00", "60.0"),
        ("", "2026-01-01T00:00:10+00:00", ""),                    # 缺失
        ("2026-01-01T00:00:00+00:00", "", ""),
        ("不是时间", "2026-01-01T00:00:10+00:00", ""),             # 解析不了
        ("2026-01-01T00:00:00", "2026-01-01T00:00:10+00:00", ""),  # 一边 aware 一边 naive
    ],
)
def test_耗时计算_读不出就留空(started, ended, expected) -> None:
    """耗时只是参考列：归因靠与环境基线对比，不为读不出时间而抛错。"""
    assert duration_seconds(started, ended) == expected


# ---------------- 列的声明（自检） ----------------


def test_现成的列都过自检() -> None:
    summary.check_columns()


def test_列声明了池子外的输入就报错() -> None:
    """`**_` 会**静默吞掉**拼错的参数名——那一列会永远取不到数、整列留空且不报错。

    所以要靠 `inspect.signature` 在开跑前拦住。这个自检本身也得被测。
    """

    def 拼错的一列(*, statz, **_) -> str:  # 想写的是 stats
        return ""

    with pytest.raises(ValueError, match="statz"):
        summary.check_columns([Column("错的列", 拼错的一列)])


def test_列函数的可变参数不算依赖() -> None:
    """`**_` 是收口用的，不是依赖声明——不然每加一份输入就要改所有列。"""

    def 只用result(*, result, **_) -> str:
        return ""

    summary.check_columns([Column("只用 result", 只用result)])


def test_选列_未知名报错不静默丢() -> None:
    with pytest.raises(ValueError, match="没有这列"):
        summary.select_columns(["没有这列"])


def test_选列为空即全选() -> None:
    assert summary.select_columns(None) == summary.COLUMNS
    assert summary.select_columns([]) == summary.COLUMNS


# ---------------- 落盘 ----------------


def test_自动跑法写正式表并追加全局(tmp_path) -> None:
    """runner 跑完调的那一下：run 内覆盖写，全局追加。"""
    run_dir = make_run(tmp_path)

    run_path, global_path = summary.write_run_and_append(run_dir, tmp_path / "runs")

    assert run_path == run_dir / SUMMARY_FILENAME
    assert global_path == tmp_path / "runs" / SUMMARY_FILENAME
    assert len(list(csv.DictReader(global_path.open(encoding="utf-8")))) == 2

    summary.write_run_and_append(run_dir, tmp_path / "runs")
    assert len(list(csv.DictReader(run_path.open(encoding="utf-8")))) == 2     # 不翻倍
    assert len(list(csv.DictReader(global_path.open(encoding="utf-8")))) == 4  # 追加


def test_主动重建不覆盖正式表(tmp_path) -> None:
    """**两种跑法靠输出文件名分开**——一次手滑不该把 runner 写的正式表冲掉。"""
    run_dir = make_run(tmp_path)
    summary.write_run_and_append(run_dir, tmp_path / "runs")
    official = (run_dir / SUMMARY_FILENAME).read_text(encoding="utf-8")

    (written,) = summary.rebuild(run_dir)

    assert written == run_dir / REBUILD_FILENAME
    assert written.read_text(encoding="utf-8") == official   # 内容一致
    assert (run_dir / SUMMARY_FILENAME).read_text(encoding="utf-8") == official  # 原封不动


def test_主动重建_可指定输出路径(tmp_path) -> None:
    run_dir = make_run(tmp_path)
    dest = tmp_path / "哪里都行.csv"

    (written,) = summary.rebuild(run_dir, out=dest)

    assert written == dest and dest.is_file()


def test_主动重建_runs根_每个run一份加全局(tmp_path) -> None:
    first = make_run(tmp_path, run_id="20260101-000000")
    second = make_run(tmp_path, run_id="20260102-000000")

    written = summary.rebuild(tmp_path / "runs")

    assert first / REBUILD_FILENAME in written
    assert second / REBUILD_FILENAME in written
    global_csv = tmp_path / "runs" / REBUILD_FILENAME
    assert global_csv in written
    # 全局按 run 顺序拼接（run_id 是时间戳，字典序即时间序）
    rows = list(csv.DictReader(global_csv.open(encoding="utf-8")))
    assert [row["session_id"] for row in rows] == ["s-A-1", "s-A-2", "s-A-1", "s-A-2"]
    assert not (tmp_path / "runs" / SUMMARY_FILENAME).exists()   # 不碰正式表


def test_主动重建_runs根时不许指定单文件输出(tmp_path) -> None:
    make_run(tmp_path)

    with pytest.raises(ValueError, match="--out 只对单个 run 目录有效"):
        summary.rebuild(tmp_path / "runs", out=tmp_path / "x.csv")


def test_重建可重复_两次结果逐字节一致(tmp_path) -> None:
    """报表是算出来的：反复重算结果必须一样。"""
    run_dir = make_run(tmp_path)

    first = summary.rebuild(run_dir)[0].read_text(encoding="utf-8")
    second = summary.rebuild(run_dir)[0].read_text(encoding="utf-8")

    assert first == second


def test_列变化时旧汇总表留档重写(tmp_path) -> None:
    """列集合变了要留档，不能拿旧表头继续写——那会**静默**错列。"""
    run_dir = make_run(tmp_path)
    dest = run_dir / REBUILD_FILENAME
    dest.write_text("时间,旧列\n2026-01-01,旧行\n", encoding="utf-8")

    summary.rebuild(run_dir)

    (backup,) = list(run_dir.glob("summary.rebuild-*.bak.csv"))
    assert "旧列" in backup.read_text(encoding="utf-8"), "旧表应整份留档"

    rows = list(csv.DictReader(dest.open(encoding="utf-8")))
    assert list(rows[0].keys()) == SUMMARY_COLUMNS      # 新表头是当前列集合


# ---------------- 命令行 ----------------


def test_命令行_run目录只重建它(tmp_path, capsys) -> None:
    run_dir = make_run(tmp_path)

    assert summary.main([str(run_dir)]) == 0

    assert (run_dir / REBUILD_FILENAME).is_file()
    assert not (run_dir / SUMMARY_FILENAME).exists()
    assert not (tmp_path / "runs" / REBUILD_FILENAME).exists()
    assert str(run_dir / REBUILD_FILENAME) in capsys.readouterr().out


def test_命令行_runs根全量重建(tmp_path) -> None:
    make_run(tmp_path, run_id="20260101-000000")
    make_run(tmp_path, run_id="20260102-000000")

    assert summary.main([str(tmp_path / "runs")]) == 0

    assert (tmp_path / "runs" / REBUILD_FILENAME).is_file()


def test_命令行_stdout只打印不落盘(tmp_path, capsys) -> None:
    run_dir = make_run(tmp_path)

    assert summary.main(["--stdout", str(run_dir)]) == 0

    out = capsys.readouterr().out
    assert not (run_dir / REBUILD_FILENAME).exists()
    assert "得分" in out and "A-1" in out


def test_命令行_只输出指定的列(tmp_path, capsys) -> None:
    run_dir = make_run(tmp_path)

    assert summary.main(["--stdout", "--columns", "类型, 得分", str(run_dir)]) == 0

    assert capsys.readouterr().out.splitlines()[0] == "类型,得分"


def test_命令行_路径不是目录时报错(tmp_path) -> None:
    with pytest.raises(SystemExit):
        summary.main([str(tmp_path / "没有这个目录")])


def test_命令行_找不到run目录时返回非零(tmp_path) -> None:
    empty = tmp_path / "runs"
    empty.mkdir()

    assert summary.main([str(empty)]) == 1
