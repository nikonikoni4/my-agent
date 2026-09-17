"""证据采集：与基线对比（表按主键比新增 / 改动 / 删除，文件比文本 diff）。

归因方式从「时间窗」换成「与基线对比」后的核心价值就在这些用例里：时间窗只能看见
新写的行，看不到被改掉的、被删掉的，也看不到没有时间列的表。

基线（`baseline` 目录）在真跑时由 `case.py` 的 a 步从环境打；这里的测试直接造一份
与环境初始态相同的基线。
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

from lifeprismevalue.evalue.evidence import collect_evidence, snapshot_baseline
from lifeprismevalue.evalue.types import Case, Precondition

# 库在数据根里的相对路径（真跑时来自 cases.yaml 的 meta.env.db.path）
DB_REL = "dataset/lifewatch_ai.db"


def make_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """造一对目录：基线（环境初始态）与环境（跑过用例的）。"""
    base = tmp_path / "baseline"
    env = tmp_path / "env"
    (base / "dataset").mkdir(parents=True)
    (env / "dataset").mkdir(parents=True)
    return base, env


def init_db(root: Path, tables: dict[str, tuple[list[str], list[tuple]]]) -> None:
    """在 root 下造库：表名 -> (列名, 行)。"""
    db = root / DB_REL
    db.parent.mkdir(parents=True, exist_ok=True)
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    for name, (columns, rows) in tables.items():
        con.execute(f"CREATE TABLE {name} ({','.join(columns)})")
        for row in rows:
            con.execute(
                f"INSERT INTO {name} VALUES ({','.join('?' * len(columns))})", tuple(row)
            )
    con.commit()
    con.close()


def make_case(*evidence: str, rules: list[str] | None = None) -> Case:
    return Case(
        id="E-1",
        type="支出记录",
        turns=[],
        evidence=list(evidence),
        rubric="应记录",
        precondition=Precondition(rules=list(rules or [])),
    )


def collect(case: Case, baseline: Path, env: Path, **kwargs) -> dict:
    return collect_evidence(
        case=case, baseline_dir=baseline, env_root=env, db_rel_path=DB_REL, **kwargs
    )


EXPENSE_COLUMNS = [
    "id", "amount", "content", "expense_category", "event_time", "created_at", "updated_at",
]


def expense_row(row_id: str, amount: float = 15.3, content: str = "午饭") -> tuple:
    return (row_id, amount, content, "餐饮", "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")


# ---------------- 表类证据 ----------------


def test_表_新增改动删除都能看出来(tmp_path) -> None:
    base, env = make_dirs(tmp_path)
    init_db(base, {"custom_expense_log": (EXPENSE_COLUMNS, [expense_row("a"), expense_row("b")])})
    init_db(env, {"custom_expense_log": (EXPENSE_COLUMNS, [expense_row("a", amount=99.0), expense_row("c")])})

    item = collect(make_case("custom_expense_log"), base, env)["targets"]["custom_expense_log"]

    assert item["kind"] == "table"
    assert item["key"] == "id"
    assert item["error"] == ""
    # 新增
    assert [row["id"] for row in item["rows"]] == ["c"]
    assert item["row_count"] == 1
    # 改动（同 id 但列值不同）
    assert len(item["changed"]) == 1
    assert item["changed"][0]["id"] == "a"
    assert item["changed"][0]["changes"]["amount"] == {"baseline": 15.3, "current": 99.0}
    # 删除
    assert [row["id"] for row in item["removed"]] == ["b"]
    assert "expense_category" in item["columns"]


def test_表_基线没有该表时按全部新增(tmp_path) -> None:
    base, env = make_dirs(tmp_path)
    init_db(base, {"other_table": (["id"], [("x",)])})
    init_db(env, {"custom_expense_log": (EXPENSE_COLUMNS, [expense_row("a"), expense_row("b")])})

    item = collect(make_case("custom_expense_log"), base, env)["targets"]["custom_expense_log"]

    assert item["row_count"] == 2
    assert "基线里读不到该表" in item["note"]


def test_表_环境没有数据库时报错(tmp_path) -> None:
    base, env = make_dirs(tmp_path)
    (env / DB_REL).unlink(missing_ok=True)

    item = collect(make_case("custom_expense_log"), base, env)["targets"]["custom_expense_log"]

    assert item["row_count"] == 0
    assert "数据库不存在" in item["error"]


def test_表_环境没声明库时报错(tmp_path) -> None:
    """环境配置里没写 db：表类证据要明说「没声明」，不能去猜一个路径。"""
    base, env = make_dirs(tmp_path)

    evidence = collect_evidence(
        case=make_case("custom_expense_log"),
        baseline_dir=base,
        env_root=env,
        db_rel_path="",
    )

    assert "未声明数据库" in evidence["targets"]["custom_expense_log"]["error"]


def test_表_表不存在时报错(tmp_path) -> None:
    base, env = make_dirs(tmp_path)
    init_db(base, {"custom_expense_log": (EXPENSE_COLUMNS, [])})
    init_db(env, {"custom_expense_log": (EXPENSE_COLUMNS, [])})

    item = collect(make_case("no_such_table"), base, env)["targets"]["no_such_table"]

    assert "表不存在" in item["error"]


def test_表_无id列时退化为集合差并注明(tmp_path) -> None:
    base, env = make_dirs(tmp_path)
    columns = ["date", "habit"]
    init_db(base, {"habit_checkins": (columns, [("2026-01-01", "跑步")])})
    init_db(
        env,
        {"habit_checkins": (columns, [("2026-01-01", "跑步"), ("2026-01-02", "阅读")])},
    )

    item = collect(make_case("habit_checkins"), base, env)["targets"]["habit_checkins"]

    assert item["key"] == ""
    assert [row["habit"] for row in item["rows"]] == ["阅读"]
    assert item["removed"] == []
    assert "无 id 列" in item["note"]


def test_表_无id列时删除也看得出来(tmp_path) -> None:
    base, env = make_dirs(tmp_path)
    columns = ["date", "habit"]
    init_db(base, {"habit_checkins": (columns, [("2026-01-01", "跑步")])})
    init_db(env, {"habit_checkins": (columns, [])})

    item = collect(make_case("habit_checkins"), base, env)["targets"]["habit_checkins"]

    assert item["row_count"] == 0
    assert [row["habit"] for row in item["removed"]] == ["跑步"]


# ---------------- 文件类证据 ----------------


def test_文件_新增文件是_new_file(tmp_path) -> None:
    base, env = make_dirs(tmp_path)
    target = env / "diary" / "2026" / "01" / "2026-01-01.md"
    target.parent.mkdir(parents=True)
    target.write_text("上午写代码\n下午看书\n", encoding="utf-8")

    item = collect(make_case("diary/2026/01/2026-01-01.md"), base, env)["targets"][
        "diary/2026/01/2026-01-01.md"
    ]

    assert item["kind"] == "file"
    assert item["exists"] is True
    assert item["new_file"] is True
    assert item["deleted"] is False
    assert item["changed"] is True
    assert item["added_lines"] == 2
    assert "+上午写代码" in item["diff"]


def test_文件_改动给出_git_风格_diff(tmp_path) -> None:
    base, env = make_dirs(tmp_path)
    for root in (base, env):
        path = root / "agent" / "chat" / "custom_prompt.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 规则\n", encoding="utf-8")
    (env / "agent" / "chat" / "custom_prompt.md").write_text("# 规则\n1. 锻炼->打卡\n", encoding="utf-8")

    item = collect(make_case("agent/chat/custom_prompt.md"), base, env)["targets"][
        "agent/chat/custom_prompt.md"
    ]

    assert item["changed"] is True
    assert item["new_file"] is False
    assert item["diff"].startswith("--- baseline\n+++ current")
    assert "+1. 锻炼->打卡" in item["diff"]


def test_文件_被删掉也算改动(tmp_path) -> None:
    base, env = make_dirs(tmp_path)
    path = base / "user" / "user.md"
    path.parent.mkdir(parents=True)
    path.write_text("只读内容", encoding="utf-8")

    item = collect(make_case("user/user.md"), base, env)["targets"]["user/user.md"]

    assert item["exists"] is False
    assert item["deleted"] is True
    assert item["changed"] is True
    assert item["removed_lines"] == 1


def test_文件_未改动时_changed_为_false(tmp_path) -> None:
    base, env = make_dirs(tmp_path)
    for root in (base, env):
        path = root / "agent" / "chat" / "custom_prompt.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 规则\n", encoding="utf-8")

    item = collect(make_case("agent/chat/custom_prompt.md"), base, env)["targets"][
        "agent/chat/custom_prompt.md"
    ]

    assert item["changed"] is False
    assert item["diff"] == ""


def test_文件_非文本文件不比对(tmp_path) -> None:
    base, env = make_dirs(tmp_path)
    path = env / "user" / "binary.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00\x01")

    item = collect(make_case("user/binary.md"), base, env)["targets"]["user/binary.md"]

    assert item["changed"] is False
    assert "非文本" in item["note"]


def test_文件_路径占位符按给定日期解析(tmp_path) -> None:
    base, env = make_dirs(tmp_path)
    moment = datetime.datetime(2026, 3, 4, 12, 0)
    target = env / "diary" / "2026" / "03" / "2026-03-04.md"
    target.parent.mkdir(parents=True)
    target.write_text("今天的事\n", encoding="utf-8")

    evidence = collect(make_case("diary/<year>/<month>/<date>.md"), base, env, now=moment)

    item = evidence["targets"]["diary/<year>/<month>/<date>.md"]
    assert item["resolved_path"] == "diary/2026/03/2026-03-04.md"
    assert item["changed"] is True


# ---------------- 全树扫描 ----------------


def test_全树扫描_收进未声明的改动并排除已声明的(tmp_path) -> None:
    base, env = make_dirs(tmp_path)
    for root in (base, env):
        (root / "user").mkdir(parents=True)
        (root / "user" / "user.md").write_text("只读内容", encoding="utf-8")
    # 已声明且改动
    (env / "user" / "user.md").write_text("agent 改了这里", encoding="utf-8")
    # 未声明且改动
    (env / "user" / "notes.md").write_text("顺手写的\n", encoding="utf-8")

    evidence = collect(make_case("user/user.md"), base, env)

    other = evidence["other_changed_files"]
    assert "user/notes.md" in other
    assert other["user/notes.md"]["new_file"] is True
    assert "user/user.md" not in other


def test_全树扫描_被删的文件也收进来(tmp_path) -> None:
    base, env = make_dirs(tmp_path)
    (base / "user").mkdir(parents=True)
    (base / "user" / "old.md").write_text("旧文件\n", encoding="utf-8")

    evidence = collect(make_case("自定义目标"), base, env, scan_other_changed_files=False)

    assert evidence["other_changed_files"] == {}

    evidence = collect(make_case("自定义目标"), base, env)
    assert evidence["other_changed_files"]["user/old.md"]["deleted"] is True


def test_可以关掉全树扫描(tmp_path) -> None:
    base, env = make_dirs(tmp_path)
    (env / "user").mkdir(parents=True)
    (env / "user" / "notes.md").write_text("改了\n", encoding="utf-8")

    evidence = collect(make_case("custom_expense_log"), base, env, scan_other_changed_files=False)

    assert evidence["other_changed_files"] == {}


def test_顶层字段(tmp_path) -> None:
    base, env = make_dirs(tmp_path)

    evidence = collect(make_case("custom_expense_log", rules=["锻炼->每日锻炼"]), base, env)

    assert evidence["case_id"] == "E-1"
    assert evidence["precondition"] == {"rules": ["锻炼->每日锻炼"]}
    assert set(evidence["targets"]) == {"custom_expense_log"}


# ---------------- 基线快照 ----------------

def test_snapshot_baseline_整份存下环境当前的样子(tmp_path) -> None:
    env = tmp_path / "env"
    (env / "agent").mkdir(parents=True)
    (env / "agent" / "custom_prompt.md").write_text("# 规则\n", encoding="utf-8")
    init_db(env, {"custom_expense_log": (EXPENSE_COLUMNS, [expense_row("a")])})

    baseline = tmp_path / "case" / "baseline"
    snapshot_baseline(env, baseline)

    assert (baseline / "agent" / "custom_prompt.md").exists()
    assert (baseline / DB_REL).is_file()


def test_snapshot_baseline_重建时不留上一次的残留(tmp_path) -> None:
    """同一个目录被重复使用时（重跑一条用例）：旧快照不能被留下来混进对比。"""
    env = tmp_path / "env"
    env.mkdir()
    (env / "今天的.md").write_text("现在的样子\n", encoding="utf-8")
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "上一次的.md").write_text("旧快照\n", encoding="utf-8")

    snapshot_baseline(env, baseline)

    assert (baseline / "今天的.md").exists()
    assert not (baseline / "上一次的.md").exists()


def test_快照之后与基线对比_只看见快照之后写的东西(tmp_path) -> None:
    """这一版归因的全貌：基线 = 环境初始态，改动 = 只有环境侧多出来的那部分。"""
    env = tmp_path / "env"
    (env / "agent" / "chat").mkdir(parents=True)
    (env / "agent" / "chat" / "custom_prompt.md").write_text("# 规则\n", encoding="utf-8")
    init_db(env, {"custom_expense_log": (EXPENSE_COLUMNS, [expense_row("旧")])})
    baseline = tmp_path / "baseline"

    snapshot_baseline(env, baseline)                      # a 步：先打基线
    init_db(env, {"custom_expense_log": (EXPENSE_COLUMNS, [expense_row("旧"), expense_row("新")])})
    (env / "agent" / "chat" / "custom_prompt.md").write_text(
        "# 规则\n1. 锻炼->打卡\n", encoding="utf-8"
    )

    evidence = collect(
        make_case("custom_expense_log", "agent/chat/custom_prompt.md"), baseline, env
    )

    assert [row["id"] for row in evidence["targets"]["custom_expense_log"]["rows"]] == ["新"]
    assert evidence["targets"]["agent/chat/custom_prompt.md"]["changed"] is True
