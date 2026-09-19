"""summary：从 run 目录里的产物重建 `summary.csv`（独立模块，可单独跑）。

**为什么要独立**：`summary.csv` 是一张**报表**，不是事实。事实是用例目录里的那些产物，
跑完即冻结。报表是从事实**算出来**的，所以它该能改字段、删字段、随时重算，而**不必重跑
一次评测**。判断这件事有没有做到，有一句可执行的检验：

    把 `runs/` 下所有 `summary.csv` 全删掉，重建能不能得到一模一样的内容？

**数据从哪来**（用例目录一级）：

| 列的内容 | 从哪来 | 谁实现 |
| --- | --- | --- |
| 起止时间 | `result.json` | `case.py:_write_result` —— **只有这三个字段推不出来**，故只有它进这份回执 |
| 是否通过 / 得分 / 理由 | `judge.json` + `session_under_test.jsonl` 的 `turn/end` | `case.py:conclusion` |
| 用了哪些 agent / 各 agent 的模型 | `case.yaml` + 各 `session_*.jsonl` | `case.py:collect_agent_info` |
| 类型 / 内容摘要 / 多轮 | `case.yaml` | `caseload.case_from_snapshot` + `case.py:content_summary` |
| session_id | `session_under_test.jsonl` 的 `meta_data` | `case.py:read_session_id` |
| 三个版本轴 / 大类 id | run 级 `run.json` | —— |

**派生一律调用 `case.py` 里已有的那些函数，不在这里重写。** 两边各判一次必然漂，而漂的
表现是「报表的结论和运行时的结论对不上」——最难查的一类错。宁可让本模块多绕一步 import
（反序列化一份 `Case` 出来），也不留第二份实现。

**两种跑法，靠输出文件名分开**：

| 谁跑 | 写到哪 |
| --- | --- |
| 自动（runner 跑完调 `write_run_and_append`） | `summary.csv`（正式表） |
| 主动（命令行 `rebuild`） | **`summary.rebuild.csv`**，或用 `--out` 指定 |

主动重建**默认不覆盖正式表**，所以它天然是「旁路对拍」：重建到旁边，与正式表 diff 一下
——一致就说明解耦成立，不一致当场看见。要用重建结果替换正式表时，`--out` 显式指过去。

命令行：

    python -m lifeprismevalue.evalue.summary <path>
      <path> = 某个 run 目录   → 只重建这一个
      <path> = runs 根目录     → 重建其下每个 run，再拼出全局总表
      --out 路径            输出到指定文件（只对单个 run 目录有效）
      --columns 类型,得分    只输出这几列（默认全选）
      --stdout              只打印，不写文件

列的口径见 `lifeprismTestData/README.md` 的「summary.csv 字段」一节；
时间类术语见 `src/lifeprismevalue/CONTEXT.md`。
"""

from __future__ import annotations

import argparse
import csv
import datetime
import inspect
import json
import logging
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from lifeprismevalue.evalue.case import (
    JUDGE,
    SIMULATOR,
    UNDER_TEST,
    collect_agent_info,
    conclusion,
    content_summary,
    dumped_session_path,
    read_session_id,
    read_turn_results,
    under_test_failure,
)
from lifeprismevalue.evalue.caseload import CaseLoadError, case_from_snapshot
from lifeprismevalue.evalue.types import (
    CASE_FILENAME,
    EVIDENCE_FILENAME,
    JUDGE_FILENAME,
    RESULT_FILENAME,
    RUN_FILENAME,
    STATS_FILENAME,
    Case,
)
from lifeprismevalue.versions import AXIS_PROMPT, AXIS_REACT, AXIS_TOOLS

logger = logging.getLogger(__name__)

# 报表文件名：正式表由 runner 写，重建表由命令行写（两者永不互相覆盖）
SUMMARY_FILENAME = "summary.csv"
REBUILD_FILENAME = "summary.rebuild.csv"

# 用例目录名的形状：`<编号>_<用例id>`，如 `000_S-1`。编号保证排序稳定。
CASE_DIR_PATTERN = re.compile(r"^\d{3}_")

# 列函数能声明的输入名。前六项是**某份产物**（run 级一项 + 用例目录五项），
# 后四项是**由产物现算**（实现都在 case.py，本模块不重写）。
INPUT_NAMES = (
    "run",          # run.json（run 级）
    "case",         # case.yaml，已反序列化成 Case 对象
    "result",       # result.json，只装推不出来的三样（见 case.py:_write_result）
    "evidence",     # evidence.json
    "judge",        # judge.json（裁判原话）
    "stats",        # stats.json
    "agents",       # 本次启用了哪些 agent（由 case.yaml 判断）
    "models",       # 各 agent 的模型名（由 session_*.jsonl 的 request/header）
    "conclusion",   # (是否通过, 得分, 理由)，由 conclusion() 算
    "session_id",   # 被测评 agent 的 session_id（由 session_under_test.jsonl 的 meta_data）
)

# 读取用到的产物文件名（`case` 在磁盘上是 case.yaml）
CASE_FILES = {
    "result": RESULT_FILENAME,
    "evidence": EVIDENCE_FILENAME,
    "judge": JUDGE_FILENAME,
    "stats": STATS_FILENAME,
}


# ---------------- 读入层 ----------------


def _load(path: Path) -> Any | None:
    """读一份产物；不存在或读不动返回 None（**不抛**）。

    为什么容错：重建一个残缺的目录（老 run 没有 `result.json`、子进程提前崩、统计失败）
    不该让整次重建崩掉——缺的那份按「没有数据」处理，格子留空。

    只以只读方式打开：本模块对产物**只读不写**，唯一会写的是它自己的报表。列函数拿到的是
    数据而不是文件对象，所以这条只读性在结构上就成立，不需要靠锁去保证。
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("读不出 %s，按无数据处理（%s: %s）", path, type(e).__name__, e)
        return None
    try:
        if path.suffix in {".yaml", ".yml"}:
            return yaml.safe_load(text)
        return json.loads(text)
    except (ValueError, yaml.YAMLError) as e:  # ValueError 覆盖 json.JSONDecodeError
        logger.warning("解析不了 %s，按无数据处理（%s: %s）", path, type(e).__name__, e)
        return None


@dataclass
class CaseFacts:
    """一条用例的全部输入（读不出的那一份是 None）。

    字段名就是列函数能声明的输入名（见 `INPUT_NAMES`）。后四项是**现算**的，不是文件：
    这样列函数只做取值，推导逻辑留在 `case.py` 一处。
    """

    case_dir: Path
    run: Any = None
    case: Case | None = None
    result: Any = None
    evidence: Any = None
    judge: Any = None
    stats: Any = None
    agents: dict[str, bool] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)
    conclusion: tuple[bool | None, int | None, str] = (None, None, "")
    session_id: str = ""

    def inputs(self) -> dict[str, Any]:
        """摊成「输入名 -> 数据」；列函数就是拿它当关键字参数被调用。"""
        return {name: getattr(self, name) for name in INPUT_NAMES}


def read_case(case_dir: Path, run: Any = None) -> CaseFacts:
    """读一条用例目录，并把派生的那几项一次算好。

    `run` 是 run 级的那份（由调用方读一次后传进来）。派生全部调 `case.py` 的函数——
    与跑的时候同一份实现，所以两边不会给出不同的结论。
    """
    facts = CaseFacts(case_dir=case_dir, run=run)

    raw_case = _load(case_dir / CASE_FILENAME)
    if raw_case is not None:
        try:
            facts.case = case_from_snapshot(raw_case)
        except CaseLoadError as e:
            # 快照读不回来只影响依赖它的几列，不该让整次重建崩掉
            logger.warning("用例快照读不回来，按无快照处理（%s）：%s", case_dir, e)

    for name, filename in CASE_FILES.items():
        setattr(facts, name, _load(case_dir / filename))
    if facts.result is None:
        # 回执是每条用例都该有的（case.py 写，子进程没起来时 runner 补）。缺了就是缺了，
        # 值得点名——不像 judge.json 那样可以名正言顺地没有。
        logger.warning("用例目录缺 %s，时间 / 耗时两列会留空：%s", RESULT_FILENAME, case_dir)

    if facts.case is not None:
        facts.agents, facts.models = collect_agent_info(facts.case, case_dir)

    session_path = dumped_session_path(case_dir, UNDER_TEST)
    facts.session_id = read_session_id(session_path)
    facts.conclusion = conclusion(
        facts.case, facts.judge, under_test_failure(read_turn_results(session_path))
    )
    # 回执里的 error 优先：用例级异常（agent 工厂抛错、超时）的原文别处没有，推导只能给出
    # 「未判定」，那会把「环境炸了」说成「没判」。其余路径下 error 与推导出的理由本来就同值。
    error = _text(facts.result, "error")
    if error:
        passed, score, _ = facts.conclusion
        facts.conclusion = (passed, score, error)

    return facts


def case_dirs(run_dir: Path) -> list[Path]:
    """一个 run 下的全部用例目录，两层：`<大类>/<编号_用例id>/`，按目录名排序。

    只认 `NNN_` 开头的子目录，所以 `envs/`、`ipc/`、`logs/`、`sessions/` 这些 run 级目录
    不会被误当成用例（它们的子目录名不匹配）。**不列 run 级目录名**是有意的：那属于
    runner 的知识，在这里再列一份只会跟着漂移。
    """
    if not run_dir.is_dir():
        return []
    return [
        case_dir
        for group in sorted(run_dir.iterdir())
        if group.is_dir()
        for case_dir in sorted(group.iterdir())
        if case_dir.is_dir() and CASE_DIR_PATTERN.match(case_dir.name)
    ]


def run_dirs(root: Path) -> list[Path]:
    """runs 根下的全部 run 目录（含 `run.json` 的），按名字排序。

    run_id 是 `YYYYmmdd-HHMMSS` 时间戳，所以字典序就是时间序。
    """
    if not root.is_dir():
        return []
    return [
        child
        for child in sorted(root.iterdir())
        if child.is_dir() and (child / RUN_FILENAME).is_file()
    ]


# ---------------- 取值小工具 ----------------


def _sub(mapping: Any, key: str) -> Any:
    """取一层子字段；不是 dict 就返回 None。"""
    return mapping.get(key) if isinstance(mapping, Mapping) else None


def _text(mapping: Any, key: str) -> str:
    """取一个字符串字段；取不到或为 None 返回空串。

    输入是外部边界（产物可能缺、结构可能变），所以一律取值不下标。
    """
    value = _sub(mapping, key)
    return "" if value is None else str(value)


def _flag(value: Any) -> str:
    """bool -> Y/N。

    **只在确定有值时调用**：None 落成空串（「不知道」不是「没用」），见各列函数的写法。
    """
    return "Y" if value else "N"


def _version_name(run: Any, axis: str) -> str:
    """从 run.json 的 `versions` 里取某个版本轴的版本名。

    快照里每个轴可能是 `{"version": ...}` 也可能是裸字符串，两种都收。
    """
    value = _sub(_sub(run, "versions"), axis)
    if isinstance(value, Mapping):
        return str(value.get("version", ""))
    return "" if value is None else str(value)


def duration_seconds(started_at: str, ended_at: str) -> str:
    """会话耗时（秒，保留 1 位小数）；时间缺失或解析不了返回空串。

    两端由 `case.py` 在跑 under_test 之前 / 之后打点，量的就是**被测评 agent 的运行
    时长**——裁判与统计那两段是评测自身的开销，混进来会让不同 run 的耗时不可比。

    ⚠️ 执行通道失败的那一条另当别论：子进程压根没跑起来，起止点只能由内核打在**整个子
    进程**上，所以那一行的「耗时(s)」量的是子进程而不是 agent。量具坏了的时候看得见，
    好过看不见。

    归因靠与环境基线对比、不靠时间，所以耗时只是参考列：读不出来（时间缺失、格式不对、
    一边带时区一边不带）就留空，不为它抛错。
    """
    if not started_at or not ended_at:
        return ""
    try:
        start = datetime.datetime.fromisoformat(started_at)
        end = datetime.datetime.fromisoformat(ended_at)
        return f"{(end - start).total_seconds():.1f}"
    except (ValueError, TypeError):
        # ValueError: 不是 ISO 8601；TypeError: 一边 aware 一边 naive，相减无定义
        return ""


# ---------------- 出表层：列声明 ----------------


@dataclass(frozen=True)
class Column:
    """一列：名字 + 取数函数。

    取数函数用关键字参数声明自己读哪一份输入（见 `INPUT_NAMES`），返回什么就写什么；
    返回 None 一律落成空串。**要加一列就写一个函数、在这里加一行**——读目录、落盘、
    runner 三处都不用动。
    """

    name: str
    get: Callable[..., Any]


# 下面这组函数一律写 `*, 需要的输入, **_`：
#   参数名 = 依赖的那份数据，一眼可读；
#   `**_`  = 收掉用不到的输入，免得每加一份输入就要改所有列。
# 拼错名字会被 check_columns() 拦下——否则那一列会**静默全空**。


def _col_时间(*, result, **_) -> str:
    """绝对时间锚点：优先会话结束时刻（与「耗时(s)」的右端是同一个点）。"""
    return _text(result, "ended_at") or _text(result, "started_at")


def _col_提示词版本(*, run, **_) -> str:
    return _version_name(run, AXIS_PROMPT)


def _col_工具版本(*, run, **_) -> str:
    return _version_name(run, AXIS_TOOLS)


def _col_agent版本(*, run, **_) -> str:
    return _version_name(run, AXIS_REACT)


def _col_版本(*, run, **_) -> str:
    """用例来源（大类 id）。它是 run 级的，所以取自 run.json 而不是用例目录。"""
    return _text(run, "cases_id")


def _col_under_test(*, agents, **_) -> str:
    # 取不到就是空，**不是 N**：「不知道用没用」与「没用」是两回事
    return "" if UNDER_TEST not in agents else _flag(agents[UNDER_TEST])


def _col_simulator(*, agents, **_) -> str:
    return "" if SIMULATOR not in agents else _flag(agents[SIMULATOR])


def _col_judge(*, agents, **_) -> str:
    return "" if JUDGE not in agents else _flag(agents[JUDGE])


def _col_under_test模型(*, models, **_) -> str:
    return models.get(UNDER_TEST, "")


def _col_simulator模型(*, models, **_) -> str:
    return models.get(SIMULATOR, "")


def _col_judge模型(*, models, **_) -> str:
    return models.get(JUDGE, "")


def _col_session_id(*, session_id, **_) -> str:
    return session_id


def _col_类型(*, case, **_) -> str:
    return case.type if case is not None else ""


def _col_测试内容摘要(*, case, **_) -> str:
    return content_summary(case) if case is not None else ""


def _col_测试结果摘要(*, conclusion, **_) -> str:
    return conclusion[2]


def _col_是否通过(*, conclusion, **_) -> str:
    """未判定（judge.mode=none / 运行未正常结束）留空——**空与 N 不是一回事**。"""
    passed = conclusion[0]
    return "" if passed is None else _flag(passed)


def _col_得分(*, conclusion, **_) -> str:
    """未判定或裁判没给分留空——**空与 0 不是一回事**（0 是「判了且不达标」）。"""
    score = conclusion[1]
    return "" if score is None else str(score)


def _col_多轮(*, case, **_) -> str:
    return "" if case is None else _flag(case.multi_turn)


def _col_耗时(*, result, **_) -> str:
    return duration_seconds(_text(result, "started_at"), _text(result, "ended_at"))


# 列的顺序就是 CSV 的列顺序。标识信息（版本 + 本次用了哪些 agent + 各 agent 的模型）
# 排在前面，便于扫表；后面是本次运行的结果与耗时。
COLUMNS = [
    Column("时间", _col_时间),
    Column("提示词版本", _col_提示词版本),
    Column("工具版本", _col_工具版本),
    Column("agent版本", _col_agent版本),
    Column("版本", _col_版本),
    Column("under_test", _col_under_test),
    Column("simulator", _col_simulator),
    Column("judge", _col_judge),
    Column("under_test模型", _col_under_test模型),
    Column("simulator模型", _col_simulator模型),
    Column("judge模型", _col_judge模型),
    Column("session_id", _col_session_id),
    Column("类型", _col_类型),
    Column("测试内容摘要", _col_测试内容摘要),
    Column("测试结果摘要", _col_测试结果摘要),
    Column("是否通过", _col_是否通过),
    Column("得分", _col_得分),
    Column("多轮(Y/N)", _col_多轮),
    Column("耗时(s)", _col_耗时),
]

# 列名 -> 声明，供 `select_columns` 与自检使用
COLUMNS_BY_NAME = {column.name: column for column in COLUMNS}

# CSV 的表头（= 列声明表的名字列）。单独给个名字，是因为它是这张表的**对外契约**：
# 文档、测试、读表的人认的都是它。
SUMMARY_COLUMNS = [column.name for column in COLUMNS]


def check_columns(columns: Iterable[Column] = COLUMNS) -> None:
    """自检：每列声明的输入名都得在池子里；不在就报错，别让它静默全空。

    要防的是 `**_` 的副作用——它会把**拼错的参数名**一并吞掉。写成
    `def col(*, stat, **_)` 而池子里叫 `stats`，这一列就永远取不到数、整列留空，
    而且不报错。这个仓库最怕的正是这种失败（见 `_backup_if_header_changed`），
    所以在开跑前拦。

    顺带：`**_` 之外带默认值的参数不算依赖（真给了也会被传进去，但不强制）。
    """
    known = set(INPUT_NAMES)
    for column in columns:
        declared = {
            name
            for name, param in inspect.signature(column.get).parameters.items()
            if param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)
            and param.default is param.empty
        }
        unknown = declared - known
        if unknown:
            raise ValueError(
                f"列「{column.name}」声明了池子里没有的输入 {sorted(unknown)}；"
                f"可用的是 {sorted(known)}"
            )


def select_columns(names: Sequence[str] | None) -> list[Column]:
    """按名字挑列；`None` 或空 = **全选**（默认）。

    顺序一律**按声明表**，不按传进来的顺序：两张表顺序不一致会让 CSV 列错位，而且不报错。
    名字认不出直接报错，不静默丢——静默丢列和静默错列一样难查。
    """
    if not names:
        return list(COLUMNS)
    unknown = [name for name in names if name not in COLUMNS_BY_NAME]
    if unknown:
        raise ValueError(f"未知的列名 {unknown}；可选的是 {[c.name for c in COLUMNS]}")
    selected = set(names)
    return [column for column in COLUMNS if column.name in selected]


# ---------------- 算表 ----------------


def _format(value: Any) -> str:
    """列函数返回 None 一律落成空串（CSV 里「没有」就是空）。

    「列不存在」与「这一格没值」都走这条路，所以列函数不必自己处理缺失。
    """
    return "" if value is None else str(value)


def build_rows(
    run_dir: Path, *, columns: Sequence[Column] = COLUMNS
) -> list[dict[str, str]]:
    """把一个 run 目录读成若干行（顺序 = 用例目录的编号顺序）。"""
    check_columns(columns)
    run = _load(run_dir / RUN_FILENAME)
    rows: list[dict[str, str]] = []
    for case_dir in case_dirs(run_dir):
        inputs = read_case(case_dir, run).inputs()
        rows.append({column.name: _format(column.get(**inputs)) for column in columns})
    return rows


# ---------------- 落盘 ----------------


def write_csv(path: Path, rows: Sequence[Mapping[str, str]], columns: Sequence[Column]) -> None:
    """覆盖写 CSV（含表头）。"""
    names = [column.name for column in columns]
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_if_header_changed(path, names)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=names)
        writer.writeheader()
        writer.writerows(rows)


def append_csv(path: Path, rows: Sequence[Mapping[str, str]], columns: Sequence[Column]) -> None:
    """追加写 CSV（文件不存在时先写表头）。"""
    names = [column.name for column in columns]
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_if_header_changed(path, names)

    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=names)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def _backup_if_header_changed(path: Path, names: Sequence[str]) -> None:
    """表头与当前列集合不一致时，把旧文件改名留档——**读表的人靠表头认列**。

    为什么覆盖写也要查：列集合会随版本演进（加「得分」、起止时间换成「耗时」）。往旧表头
    下面继续写，无论是追加还是覆盖，旧行都会按新列的意思被读出来——**错位而且不报错**。
    与其静默改掉一批旧数据的含义，不如留档重开（`summary-<ts>.bak.csv`）。
    """
    header = _read_csv_header(path)
    if header is None or header == list(names):
        return
    backup = path.with_name(f"{path.stem}-{_timestamp_suffix()}.bak{path.suffix}")
    path.replace(backup)
    logger.warning("汇总表列已变化，旧表留档为 %s，本次按新列重写表头", backup.name)


def write_run(
    run_dir: Path, *, columns: Sequence[Column] = COLUMNS, filename: str = SUMMARY_FILENAME
) -> Path:
    """重建一个 run 的报表（默认只写那一个文件，不碰全局）。"""
    path = run_dir / filename
    write_csv(path, build_rows(run_dir, columns=columns), columns)
    return path


def append_global(
    runs_dir: Path, rows: Sequence[Mapping[str, str]], *, columns: Sequence[Column] = COLUMNS
) -> Path:
    """把本次 run 的行追加到全局总表（`runs/summary.csv`）。

    跑完自动调时用追加，保留历史行（含那些 run 目录已被删掉的旧记录）；
    主动全量重建走 `rebuild`，整份重写成 `summary.rebuild.csv`。
    """
    path = runs_dir / SUMMARY_FILENAME
    append_csv(path, rows, columns)
    return path


def write_run_and_append(
    run_dir: Path, runs_dir: Path, *, columns: Sequence[Column] = COLUMNS
) -> tuple[Path, Path]:
    """**自动**跑法：runner 跑完调的那一下。

    写的是正式表 `summary.csv`（本次 run 的 + 全局追加）。输入是**刚落盘的文件**，
    不是 runner 手里那份内存结果——走同一条路，才能保证「删掉 summary 还能重建」这件事
    一直成立。跑的时候不需要任何护栏：目标 run 刚跑出来，产物一定是齐的。
    """
    rows = build_rows(run_dir, columns=columns)
    run_path = run_dir / SUMMARY_FILENAME
    write_csv(run_path, rows, columns)
    return run_path, append_global(runs_dir, rows, columns=columns)


def rebuild(
    target: str | Path, *, column_names: Sequence[str] | None = None, out: str | Path | None = None
) -> list[Path]:
    """**主动**跑法：从命令行重建，默认写到 `summary.rebuild.csv`，**不覆盖正式表**。

    为什么换个名字：正式表是 runner 跑完写的。主动重建若默认覆盖它，一次手滑就能把历史
    数据冲掉——而且新旧表头一致时不报错、也不留档。换个名字之后，重建天然是**旁路对拍**：
    重建到旁边，与正式表 diff 一下，一致就说明解耦成立。确要替换正式表时，`out` 显式指过去。

    `target` 是 run 目录（含 `run.json`）→ 只重建它；
    是 runs 根目录 → 每个 run 各重建一份，再拼出全局总表。

    Returns:
        写出的文件路径。
    """
    target = Path(target)
    columns = select_columns(column_names)

    if (target / RUN_FILENAME).is_file():
        dest = Path(out) if out is not None else target / REBUILD_FILENAME
        write_csv(dest, build_rows(target, columns=columns), columns)
        return [dest]

    if out is not None:
        raise ValueError(f"--out 只对单个 run 目录有效，而 {target} 是一个 runs 根目录")

    written: list[Path] = []
    rows: list[dict[str, str]] = []
    for run_dir in run_dirs(target):
        run_rows = build_rows(run_dir, columns=columns)
        rows.extend(run_rows)
        written.append(write_run(run_dir, columns=columns, filename=REBUILD_FILENAME))
    global_path = target / REBUILD_FILENAME
    write_csv(global_path, rows, columns)
    written.append(global_path)
    return written


def _read_csv_header(path: Path) -> list[str] | None:
    """读 CSV 的表头行；文件不存在或读不到返回 None。"""
    if not path.is_file():
        return None
    try:
        with path.open("r", newline="", encoding="utf-8") as f:
            return next(csv.reader(f), None)
    except OSError:
        return None


def _timestamp_suffix() -> str:
    """留档文件名用的时间戳（UTC，YYYYmmdd-HHMMSS）。"""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


# ---------------- 命令行 ----------------


def _dump(rows: Sequence[Mapping[str, str]], columns: Sequence[Column]) -> None:
    """把行打到标准输出（`--stdout`）。"""
    writer = csv.DictWriter(sys.stdout, fieldnames=[column.name for column in columns])
    writer.writeheader()
    writer.writerows(rows)


def _split_names(raw: str) -> list[str]:
    """`"类型, 得分"` -> `["类型", "得分"]`；空串 -> 空列表（= 全选）。"""
    return [name.strip() for name in raw.split(",") if name.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lifeprismevalue.evalue.summary",
        description="从 run 目录里的产物重建报表（不重跑评测，也不覆盖正式表）",
    )
    parser.add_argument(
        "path", help="run 目录（只重建它）或 runs 根目录（每个 run 各一份 + 全局总表）"
    )
    parser.add_argument("--out", default="", help=f"输出到指定文件；不写 = {REBUILD_FILENAME}")
    parser.add_argument("--columns", default="", help="只输出这几列（逗号分隔）；不写 = 全选")
    parser.add_argument("--stdout", action="store_true", help="只打印到标准输出，不写文件")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    target = Path(args.path)
    if not target.is_dir():
        parser.error(f"路径不是目录：{target}")
    try:
        columns = select_columns(_split_names(args.columns))
    except ValueError as e:
        parser.error(str(e))

    # 判断是不是 run 目录看有没有 run.json——这个文件名只有一处定义
    single = (target / RUN_FILENAME).is_file()
    if not single and not run_dirs(target):
        print(f"没找到任何 run 目录（缺 {RUN_FILENAME}）：{target}", file=sys.stderr)
        return 1

    if args.stdout:
        rows: list[dict[str, str]] = []
        for run_dir in [target] if single else run_dirs(target):
            rows.extend(build_rows(run_dir, columns=columns))
        _dump(rows, columns)
        return 0

    try:
        written = rebuild(target, column_names=_split_names(args.columns), out=args.out or None)
    except ValueError as e:
        parser.error(str(e))
    for path in written:
        print(f"已重建 {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口
    raise SystemExit(main())
