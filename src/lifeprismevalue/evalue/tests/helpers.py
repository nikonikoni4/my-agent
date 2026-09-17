"""evalue 测试的共用构造件（fixture 在 conftest.py，这里只放可 import 的构造函数）。

构造原则：**能离线跑**。被测 agent 用假实现注入（fakes.py），跨进程的链路用
fake_case.py 那个假 entrypoint 走真子进程，没有 LLM 依赖。
"""

from __future__ import annotations

import datetime
import shutil
import sqlite3
from pathlib import Path

from lifeprismevalue.evalue.caseload import load_case_set
from lifeprismevalue.evalue.case import CaseContext
from lifeprismevalue.evalue.types import Case, CaseSet

# 底座里两个「会被写」的位置（DB 与规则文件）
DB_REL = "dataset/lifewatch_ai.db"
CP_REL = "agent/chat/custom_prompt.md"
BASE_CP_TEXT = "# 自定义记录规则\n\n### 支出记录规则\n- 支出类型必须从枚举里选\n"


# ---------------- base / 用例 ----------------


def make_base(root: Path) -> Path:
    """造一个极小的 base 底座（可变路径 + 一点只读内容 + 提示词版本库）。"""
    base = root / "base"
    (base / "dataset").mkdir(parents=True)
    (base / "agent" / "chat").mkdir(parents=True)
    (base / DB_REL).write_bytes(b"DB-BASELINE")
    (base / CP_REL).write_text(BASE_CP_TEXT, encoding="utf-8")
    (base / "prompts").mkdir()
    (base / "prompts" / "agent_prompts.yaml").write_text(
        "active_version: v1\n", encoding="utf-8"
    )
    (base / "user").mkdir(parents=True)
    (base / "user" / "user.md").write_text("只读内容", encoding="utf-8")
    return base


def make_env_copy(base: Path, root: Path) -> Path:
    """把 base 复制一份当环境数据根（case 级测试用；真跑时由 provider 复制）。"""
    env_root = root / "env"
    shutil.copytree(base, env_root)
    return env_root


def make_baseline(base: Path, root: Path) -> Path:
    """造一份「环境初始态快照」（= 环境本来的样子，真跑时由 case.py 的 a 步打）。

    刻意与 base 分开放：快照是独立目录，快照动作会整份覆盖它——若指向 base，
    就把底座也给覆盖了。
    """
    baseline = root / "baseline"
    shutil.copytree(base, baseline)
    return baseline


def write_cases(tmp_path: Path, body: str, name: str = "cases.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def load_cases(tmp_path: Path, body: str) -> CaseSet:
    return load_case_set(write_cases(tmp_path, body))


def load_case(tmp_path: Path, body: str, index: int = 0) -> Case:
    return load_cases(tmp_path, body).cases[index]


def make_context(
    tmp_path: Path,
    body: str,
    *,
    index: int = 0,
    env_root: Path | None = None,
    baseline_dir: Path | None = None,
    base_dir: Path | None = None,
    case_dir: Path | None = None,
    db_rel_path: str = DB_REL,
    turn_timeout: float = 5.0,
    scan_other_changed_files: bool = True,
) -> tuple[CaseContext, Path]:
    """造 base + 环境副本 + 环境基线 + 用例，返回 (ctx, case_dir)。"""
    base = base_dir or make_base(tmp_path)
    case_set = load_cases(tmp_path, body)
    ctx = CaseContext(
        case=case_set.cases[index],
        meta_id=case_set.meta.id,
        case_dir=case_dir or tmp_path / "case",
        env_root=env_root or make_env_copy(base, tmp_path),
        baseline_dir=baseline_dir or make_baseline(base, tmp_path),
        db_rel_path=db_rel_path,
        session_folder=tmp_path / "sessions",
        turn_timeout=turn_timeout,
        scan_other_changed_files=scan_other_changed_files,
    )
    return ctx, ctx.case_dir


# ---------------- 用例集（cases.yaml） ----------------

# 测试用例的 meta.env：一份用例文件一份环境声明。
# 只复制提示词目录（agent/chat/），库走整库复制——测试底座里那个「库」是占位字节，
# 不是真 sqlite，故不能走 empty 模式（那要读 schema）。
CASES_MIN = """
meta:
  id: 记录任务-99
  dataset_version: 3
  env:
    copy: [agent/, prompts/]
    db: {path: dataset/lifewatch_ai.db, mode: copy}
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
  env:
    copy: [agent/, prompts/]
    db: {path: dataset/lifewatch_ai.db, mode: copy}
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
  env:
    copy: [agent/, prompts/]
    db: {path: dataset/lifewatch_ai.db, mode: copy}
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
  env:
    copy: [agent/, prompts/]
    db: {path: dataset/lifewatch_ai.db, mode: copy}
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
  env:
    copy: [agent/, prompts/]
    db: {path: dataset/lifewatch_ai.db, mode: copy}
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

CASES_JUDGE_NONE = """
meta:
  id: 记录任务-90
  env:
    copy: [agent/, prompts/]
    db: {path: dataset/lifewatch_ai.db, mode: copy}
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

CASES_EVIDENCE = """
meta:
  id: 记录任务-92
  env:
    copy: [agent/, prompts/]
    db: {path: dataset/lifewatch_ai.db, mode: copy}
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
  env:
    copy: [agent/, prompts/]
    db: {path: dataset/lifewatch_ai.db, mode: copy}
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


def fake_cases(*ids: str, meta_id: str = "假用例-01") -> str:
    """按用例 id 生成一份 cases.yaml（假 entrypoint 按 id 前缀决定行为）。"""
    body = "".join(
        f"  - id: {cid}\n"
        f"    type: 假类型\n"
        f"    evidence: [fake_target]\n"
        f"    turns:\n"
        f"      - {{role: user, text: \"第 {i} 条\"}}\n"
        f"    rubric: 应通过\n"
        for i, cid in enumerate(ids)
    )
    return (
        f"meta:\n  id: {meta_id}\n"
        "  env:\n"
        "    copy: [agent/, prompts/]\n"
        "    db: {path: dataset/lifewatch_ai.db, mode: copy}\n"
        f"cases:\n{body}"
    )


# ---------------- 数据库 / 文件 ----------------


def init_expense_db(root: Path, rows: list[dict]) -> None:
    """在给定数据根下造一个含 custom_expense_log 的真 sqlite 库。

    rows 每项形如 {"id": "a", "amount": 15.3, "content": "午饭", "expense_category": "餐饮"}，
    缺的字段补默认值；已有同名文件（make_base 放的占位字节）先删掉。
    """
    db = root / DB_REL
    db.parent.mkdir(parents=True, exist_ok=True)
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE custom_expense_log (id TEXT, amount REAL, content TEXT, "
        "expense_category TEXT, event_time TEXT, created_at TEXT, updated_at TEXT)"
    )
    for row in rows:
        con.execute(
            "INSERT INTO custom_expense_log VALUES (?,?,?,?,?,?,?)",
            (
                row.get("id"),
                row.get("amount", 15.3),
                row.get("content", "午饭"),
                row.get("expense_category", "餐饮"),
                row.get("event_time", "2026-01-01T00:00:00+00:00"),
                row.get("created_at", "2026-01-01T00:00:00+00:00"),
                row.get("updated_at", "2026-01-01T00:00:00+00:00"),
            ),
        )
    con.commit()
    con.close()


def today_diary_rel(now: datetime.datetime | None = None) -> str:
    moment = now or datetime.datetime.now()
    return f"diary/{moment:%Y}/{moment:%m}/{moment:%Y-%m-%d}.md"
