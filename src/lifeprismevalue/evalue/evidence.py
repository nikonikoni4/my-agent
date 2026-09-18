"""证据采集：把「本次写入」从环境里捞出来，与**基线**对比。

**基线 = 这条用例环境的初始态快照**，由 `snapshot_baseline` 在跑用例之前打下（见
`case.py`）。不再拿 `base/` 当基线：环境是按声明拼的（只复制提示词、库只留几张表），
底座里那些没进环境的东西（几百篇历史日记、二十万行事件日志）会被全量 diff 判成
「本次删掉了」，裁判看到的证据就是一堆噪声。

**归因方式：与基线对比，不再用时间窗。** 环境是每条用例独占的，凡是与基线不同之处
就是本次写入——不必再依赖写入时间戳。时间窗只能看见「新写的行」，看不见「被改掉、被删掉
的东西」，也看不见「没有时间列的表」；对比基线三类都看得见。

两类目标（取值见 `lifeprismTestData/README.md` 的 `evidence`）：

- **表**（`custom_expense_log` / `mood_entries` / ...）：有 `id` 列就按 id 对齐，比出
  新增 / 改动 / 删除；没有 id 列则退化为整行多重集合差，只分新增 / 删除（改动无法与
  「一删一增」区分），并在 `note` 里注明。
- **文件**（`agent/chat/custom_prompt.md` / 日记）：文本统一 diff，`<year>` / `<month>` /
  `<date>` 占位符按当天解析；新增文件、被删文件都算改动。

另外做两次全量扫描，专治"没写在声明的位置"这类漏判：

- **文件**：收进「未被 `evidence` 声明但确实改了」的文本文件；
- **表**：收进「未被声明但被动过」的表（`other_changed_tables`）。表类证据只按声明取，
  于是"写到别的表去了"在判分时看不见——真实教训：一条"时间块备注"用例被 agent 写进了
  锻炼记录表、还顺手打了个卡，裁判只能看到"声明的那张表 0 行"，于是判成"没做"，
  而真相是"写错地方"（处置完全不同）。
"""

from __future__ import annotations

import datetime
import difflib
import hashlib
import shutil
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from lifeprismevalue.evalue.sqlite_read import open_readonly, quote_identifier, table_names
from lifeprismevalue.evalue.types import Case

# 全树对比时跳过的大文件 / 二进制后缀（避免把 DB、会话、图片当文本比）
SKIP_SUFFIXES = frozenset(
    {
        ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3",
        ".jsonl", ".pyc", ".pyo",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf",
        ".zip", ".gz", ".exe", ".dll", ".so",
    }
)
MAX_TEXT_BYTES = 1_000_000

# 统一 diff 的上下文行数（0 = 只留变更行）
DIFF_CONTEXT = 1

# 行对齐用的主键列：表普遍有它，有它才能把「改动」从「一删一增」里分出来
KEY_COLUMN = "id"

# 全库扫描的单表行数上限：超过它就只比行数、不做逐行指纹（防漏判也要有成本上限）
MAX_SCAN_ROWS = 20_000


def snapshot_baseline(env_root: str | Path, baseline_dir: str | Path) -> None:
    """把环境当下的样子整份存成基线：之后一切归因都对着它比。

    调用时机是「环境造好、`precondition` 之前」（见 `case.py`）：precondition 往
    `custom_prompt.md` 写的规则本身也是证据（有用例要判「规则文件终态」），打在其后
    就把这一步藏起来了。

    环境现在是「提示词 + 几张表」的量级（几十 KB ~ 几 MB），整份存远比维护一张
    「哪些东西算初始状态」的清单划算——那张清单会漏，漏了不报错。
    """
    baseline = Path(baseline_dir)
    if baseline.exists():
        shutil.rmtree(baseline)
    shutil.copytree(env_root, baseline)


def collect_evidence(
    *,
    case: Case,
    env_root: str | Path,
    baseline_dir: str | Path,
    db_rel_path: str,
    scan_other_changed_files: bool = True,
    now: datetime.datetime | None = None,
) -> dict:
    """导出该用例的 evidence（返回结构也直接落盘成 `evidence.json`）。

    Args:
        case: 当前用例（决定去哪些位置取证）。
        env_root: 本次用例的环境数据根（写过的）。
        baseline_dir: 这条用例环境的初始态快照（基线，见 `snapshot_baseline`）。
        db_rel_path: 库在数据根里的相对路径；空串 = 本环境没声明库（表类证据会报错，
            而不是悄悄去猜一个路径）。
        scan_other_changed_files: 是否额外全树对比，收进「未声明但确实改了」的文本文件。
            表级全量扫描不受它控制（见 `_collect_other_changed_tables`：那条信息缺失会
            直接导致误判归因，而成本有上限）。
        now: 解析路径占位符用的「今天」，默认取当前本地时间（测试可注入）。

    Returns:
        {"case_id", "precondition", "targets", "other_changed_files", "other_changed_tables"}
    """
    env_root, baseline_dir = Path(env_root), Path(baseline_dir)
    targets = {
        raw: (
            _collect_file(baseline_dir, env_root, raw, now)
            if is_file_target(raw)
            else _collect_table(baseline_dir, env_root, raw, db_rel_path)
        )
        for raw in case.evidence
    }
    return {
        "case_id": case.id,
        "precondition": {"rules": list(case.precondition.rules)},
        "targets": targets,
        "other_changed_files": (
            _collect_other_changed_files(baseline_dir, env_root, case.evidence, now)
            if scan_other_changed_files
            else {}
        ),
        "other_changed_tables": (
            _collect_other_changed_tables(
                baseline_dir / db_rel_path, env_root / db_rel_path, case.evidence
            )
            if db_rel_path
            else {}
        ),
    }


# ---------------- 表类证据 ----------------


def _collect_table(baseline_dir: Path, env_root: Path, table: str, db_rel_path: str) -> dict:
    """按主键（若有）比出环境相对基线的新增 / 改动 / 删除。"""
    if not db_rel_path:
        return _table_result(table, error="本环境未声明数据库（meta.env.db）")

    env_db = env_root / db_rel_path
    if not env_db.exists():
        return _table_result(table, error=f"数据库不存在: {db_rel_path}")

    env_rows, columns, error = _read_table(env_db, table)
    if error:
        return _table_result(table, error=error)

    baseline_db = baseline_dir / db_rel_path
    baseline_rows: list[list] = []
    note = ""
    if not baseline_db.exists():
        note = "基线没有数据库，全部行按新增处理"
    else:
        baseline_rows, _baseline_columns, baseline_error = _read_table(baseline_db, table)
        if baseline_error:
            note = f"基线里读不到该表（按空表处理）：{baseline_error}"

    added, changed, removed, diff_note = _diff_rows(env_rows, baseline_rows, columns)
    return _table_result(
        table,
        rows=added,
        columns=columns,
        changed=changed,
        removed=removed,
        note="；".join(part for part in (note, diff_note) if part),
    )


def _diff_rows(
    env_rows: list[list], baseline_rows: list[list], columns: list[str]
) -> tuple[list[dict], list[dict], list[dict], str]:
    """比出新增 / 改动 / 删除三份结果。

    有 id 列：按 id 对齐，同 id 的列值不同即「改动」（逐列给 baseline / current）。
    无 id 列：整行做多重集合差，只能分新增 / 删除，改动会表现为「一删一增」。
    """
    if KEY_COLUMN in columns:
        key_at = columns.index(KEY_COLUMN)
        baseline_map = {row[key_at]: row for row in baseline_rows}
        added, changed, removed = [], [], []
        seen: set[Any] = set()
        for row in env_rows:
            key = row[key_at]
            seen.add(key)
            counterpart = baseline_map.get(key)
            if counterpart is None:
                added.append(dict(zip(columns, row)))
                continue
            differences = {
                column: {"baseline": old, "current": new}
                for column, old, new in zip(columns, counterpart, row)
                if old != new
            }
            if differences:
                changed.append({KEY_COLUMN: key, "changes": differences})
        removed = [
            dict(zip(columns, row)) for key, row in baseline_map.items() if key not in seen
        ]
        return added, changed, removed, ""

    baseline_counter = Counter(tuple(row) for row in baseline_rows)
    env_counter = Counter(tuple(row) for row in env_rows)
    added_counter = env_counter - baseline_counter
    removed_counter = baseline_counter - env_counter
    added = [
        dict(zip(columns, row))
        for row, times in added_counter.items()
        for _ in range(times)
    ]
    removed = [
        dict(zip(columns, row))
        for row, times in removed_counter.items()
        for _ in range(times)
    ]
    return added, [], removed, f"该表无 {KEY_COLUMN} 列，按整行做集合差：改动会表现为「一删一增」"


def _read_table(db_path: Path, table: str) -> tuple[list[list], list[str], str]:
    """读整张表；返回（行, 列名, 错误）。表不存在或读失败时行/列为空。"""
    try:
        con = sqlite3.connect(db_path)
        try:
            cur = con.cursor()
            columns = [row[1] for row in cur.execute(f"PRAGMA table_info({table})")]
            if not columns:
                return [], [], f"表不存在: {table}"
            rows = [list(row) for row in cur.execute(f"SELECT * FROM {table}")]
            return rows, columns, ""
        finally:
            con.close()
    except sqlite3.Error as e:
        return [], [], f"读取失败: {e}"


def _table_result(
    table: str,
    rows: list[dict] | None = None,
    columns: list[str] | None = None,
    changed: list[dict] | None = None,
    removed: list[dict] | None = None,
    note: str = "",
    error: str = "",
) -> dict:
    """组装一条「表类证据」结果。

    `rows` / `row_count` 仍是「本次新增的行」（判读习惯不变），改动与删除另开两个字段。
    """
    added = list(rows or [])
    return {
        "kind": "table",
        "table": table,
        "columns": list(columns or []),
        "key": KEY_COLUMN if KEY_COLUMN in (columns or []) else "",
        "rows": added,
        "row_count": len(added),
        "changed": list(changed or []),
        "removed": list(removed or []),
        "note": note,
        "error": error,
    }


# ---------------- 未声明却被动过的表（全库扫描） ----------------


def _collect_other_changed_tables(
    baseline_db: Path, env_db: Path, declared: Iterable[str]
) -> dict:
    """全库扫一遍：哪些**没被声明**的表被本次运行动过。

    与 `_collect_other_changed_files` 同一个目的（防漏判），对象换成**表**：表类证据只按
    用例声明的表取，于是"写到别的表去了"在判分时完全看不见。**刻意不给开关**——这条信息
    缺失的代价是把"写错地方"误判成"没做"（两者处置完全不同），而成本有上限
    （表数 × 每表行数上限），没有关掉它的理由。

    Returns:
        {表名: {"kind", "table", "rows_baseline", "rows_current", "note"}}
    """
    if not baseline_db.is_file() or not env_db.is_file():
        return {}
    declared_tables = {item for item in declared if not is_file_target(item)}

    changed: dict[str, dict] = {}
    before_tables, after_tables = table_names(baseline_db), table_names(env_db)
    before_con, after_con = open_readonly(baseline_db), open_readonly(env_db)
    try:
        for table in sorted(before_tables | after_tables):
            if table in declared_tables:
                continue
            item = _touched_table(
                before_con,
                after_con,
                table,
                exists_before=table in before_tables,
                exists_after=table in after_tables,
            )
            if item is not None:
                changed[table] = item
    finally:
        before_con.close()
        after_con.close()
    return changed


def _touched_table(
    before_con: sqlite3.Connection,
    after_con: sqlite3.Connection,
    table: str,
    *,
    exists_before: bool,
    exists_after: bool,
) -> dict | None:
    """这张表被动过吗？没动过返回 None。

    两侧都要看：表只在一侧存在也算改动——被测工具能建自定义记录表（`create_type`），
    「本该写记录却去建了个新表」正是要靠这里露出来的那一类。
    """
    if not exists_before:
        after = _row_count(after_con, table)
        return None if after is None else _touched(table, 0, after, "本次新建的表")
    if not exists_after:
        before = _row_count(before_con, table)
        return None if before is None else _touched(table, before, 0, "本次删掉了这张表")

    before, after = _row_count(before_con, table), _row_count(after_con, table)
    if before is None or after is None:
        return None                 # 读不到就交给声明过的目标去报错，这里不猜
    if before != after:
        return _touched(table, before, after, f"行数 {before} -> {after}")
    if before > MAX_SCAN_ROWS:
        return None                 # 行数没变、表又太大：不逐行比（宁可漏这一种）
    if _fingerprint(before_con, table) != _fingerprint(after_con, table):
        return _touched(table, before, after, "行数不变，但内容有改动")
    return None


def _row_count(con: sqlite3.Connection, table: str) -> int | None:
    """表行数；读不到（表不存在等）返回 None。"""
    try:
        return con.execute(f"select count(*) from {quote_identifier(table)}").fetchone()[0]
    except sqlite3.Error:
        return None


def _fingerprint(con: sqlite3.Connection, table: str) -> str:
    """整表逐行做指纹（流式，不把行攒进内存）。

    行数相等时用它判"内容被改过"：只比行数会漏掉"改了一行但条数不变"
    （例如把某条记录的类型改掉、把余额快照的数值改掉）。
    """
    name = quote_identifier(table)
    try:
        rows = con.execute(f"select * from {name} order by rowid")
    except sqlite3.Error:
        # 没有 rowid 的表（WITHOUT ROWID）：退化成不排序，仍能比出"内容不同"
        rows = con.execute(f"select * from {name}")
    digest = hashlib.sha256()
    for row in rows:
        digest.update(repr(row).encode("utf-8", "surrogatepass"))
    return digest.hexdigest()


def _touched(table: str, before: int, after: int, delta: str) -> dict:
    """组装一条「未声明却被动了」的表记录（只说事实，怎么解读交给裁判）。"""
    return {
        "kind": "table",
        "table": table,
        "rows_baseline": before,
        "rows_current": after,
        "note": f"未被本用例声明为证据，但本次运行改动过它（{delta}）",
    }


# ---------------- 文件类证据 ----------------


def _collect_file(
    baseline_dir: Path, env_root: Path, raw: str, now: datetime.datetime | None
) -> dict:
    """采集文本类证据：解析路径占位符，与基线对比出统一 diff。"""
    rel = _resolve_path_placeholders(raw, now)
    return _file_diff(baseline_dir / rel, env_root / rel, rel)


def _collect_other_changed_files(
    baseline_dir: Path, env_root: Path, declared: Iterable[str], now: datetime.datetime | None
) -> dict:
    """全树对比基线与环境，收集声明之外被改动的文本文件（含被删的）。"""
    declared_rel = {
        _resolve_path_placeholders(item, now) for item in declared if is_file_target(item)
    }
    changed: dict[str, dict] = {}
    baseline_files = _iter_candidate_files(baseline_dir)
    env_files = _iter_candidate_files(env_root)
    for rel in sorted(set(baseline_files) | set(env_files)):
        if rel in declared_rel:
            continue
        item = _file_diff(baseline_dir / rel, env_root / rel, rel)
        if item["changed"]:
            changed[rel] = item
    return changed


def _file_diff(baseline_path: Path, current_path: Path, rel: str) -> dict:
    """对比同一相对路径在基线与环境中的文本，产出统一 diff 结构。"""
    exists = current_path.is_file()
    result = {
        "kind": "file",
        "resolved_path": rel,
        "exists": exists,
        "changed": False,
        "new_file": False,
        "deleted": False,
        "diff": "",
        "added_lines": 0,
        "removed_lines": 0,
        "note": "",
    }

    current_text = _read_text(current_path) if exists else None
    baseline_text = _read_text(baseline_path)
    if exists and current_text is None:
        result["note"] = "非文本文件，未比对"
        return result

    if not exists:
        # 基线里有、现在没了 = 本次删掉了（也算改动，否则「删了不该删的」会被漏判）
        if baseline_text is not None:
            result["changed"] = True
            result["deleted"] = True
            result["removed_lines"] = len(baseline_text.splitlines())
        return result

    result["new_file"] = baseline_text is None
    before = baseline_text if baseline_text is not None else ""
    if before == current_text:
        return result

    diff_lines = list(
        difflib.unified_diff(
            before.splitlines(),
            current_text.splitlines(),
            fromfile="baseline",
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


def is_file_target(value: str) -> bool:
    """evidence 取值是「文件」还是「表」：带路径分隔符或以 .md 结尾的按文件处理。

    公开出去是因为"装配层开跑前的自检"也要按同一条规则分流（表类目标必须落在库里），
    两处各写一份就会漂移，漂移了不报错。
    """
    return value.endswith(".md") or "/" in value or "\\" in value


def _resolve_path_placeholders(raw: str, now: datetime.datetime | None = None) -> str:
    """把 evidence 里的 `<year>/<month>/<date>` 占位符解析为具体日期（按本地日期）。"""
    today = now or datetime.datetime.now()
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
