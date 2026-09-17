"""证据采集：把「本次写入」从环境里捞出来，与只读底座对比。

**归因方式：与底座对比，不再用时间窗。** 环境是每条用例独占的，凡是与 `base/` 不同之处
就是本次写入——不必再依赖写入时间戳。时间窗只能看见「新写的行」，看不见「被改掉、被删掉
的东西」，也看不见「没有时间列的表」；对比底座三类都看得见。

两类目标（取值见 `lifeprismTestData/README.md` 的 `evidence`）：

- **表**（`custom_expense_log` / `mood_entries` / ...）：有 `id` 列就按 id 对齐，比出
  新增 / 改动 / 删除；没有 id 列则退化为整行多重集合差，只分新增 / 删除（改动无法与
  「一删一增」区分），并在 `note` 里注明。
- **文件**（`agent/chat/custom_prompt.md` / 日记）：文本统一 diff，`<year>` / `<month>` /
  `<date>` 占位符按当天解析；新增文件、被删文件都算改动。

另外做一次全树扫描，收进「未被 `evidence` 声明但确实改了」的文本文件（防漏判）。
"""

from __future__ import annotations

import datetime
import difflib
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from lifeprismevalue.evalue.types import Case

# 数据库相对数据根的路径（lifeprism 的结构化存储）
DB_REL_PATH = "dataset/lifewatch_ai.db"

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


def collect_evidence(
    *,
    case: Case,
    env_root: str | Path,
    base_dir: str | Path,
    scan_other_changed_files: bool = True,
    now: datetime.datetime | None = None,
) -> dict:
    """导出该用例的 evidence（返回结构也直接落盘成 `evidence.json`）。

    Args:
        case: 当前用例（决定去哪些位置取证）。
        env_root: 本次用例的环境数据根（写过的）。
        base_dir: 只读底座（基线）。
        scan_other_changed_files: 是否额外全树对比，收进「未声明但确实改了」的文本文件。
        now: 解析路径占位符用的「今天」，默认取当前本地时间（测试可注入）。

    Returns:
        {"case_id", "precondition", "targets", "other_changed_files"}
    """
    env_root, base_dir = Path(env_root), Path(base_dir)
    targets = {
        raw: (
            _collect_file(base_dir, env_root, raw, now)
            if _is_file_target(raw)
            else _collect_table(base_dir, env_root, raw)
        )
        for raw in case.evidence
    }
    return {
        "case_id": case.id,
        "precondition": {"rules": list(case.precondition.rules)},
        "targets": targets,
        "other_changed_files": (
            _collect_other_changed_files(base_dir, env_root, case.evidence, now)
            if scan_other_changed_files
            else {}
        ),
    }


# ---------------- 表类证据 ----------------


def _collect_table(base_dir: Path, env_root: Path, table: str) -> dict:
    """按主键（若有）比出环境相对底座的新增 / 改动 / 删除。"""
    env_db = env_root / DB_REL_PATH
    if not env_db.exists():
        return _table_result(table, error=f"数据库不存在: {DB_REL_PATH}")

    env_rows, columns, error = _read_table(env_db, table)
    if error:
        return _table_result(table, error=error)

    base_db = base_dir / DB_REL_PATH
    base_rows: list[list] = []
    note = ""
    if not base_db.exists():
        note = "底座没有数据库，全部行按新增处理"
    else:
        base_rows, _base_columns, base_error = _read_table(base_db, table)
        if base_error:
            note = f"底座里读不到该表（按空表处理）：{base_error}"

    added, changed, removed, diff_note = _diff_rows(env_rows, base_rows, columns)
    return _table_result(
        table,
        rows=added,
        columns=columns,
        changed=changed,
        removed=removed,
        note="；".join(part for part in (note, diff_note) if part),
    )


def _diff_rows(
    env_rows: list[list], base_rows: list[list], columns: list[str]
) -> tuple[list[dict], list[dict], list[dict], str]:
    """比出新增 / 改动 / 删除三份结果。

    有 id 列：按 id 对齐，同 id 的列值不同即「改动」（逐列给 base / current）。
    无 id 列：整行做多重集合差，只能分新增 / 删除，改动会表现为「一删一增」。
    """
    if KEY_COLUMN in columns:
        key_at = columns.index(KEY_COLUMN)
        base_map = {row[key_at]: row for row in base_rows}
        added, changed, removed = [], [], []
        seen: set[Any] = set()
        for row in env_rows:
            key = row[key_at]
            seen.add(key)
            counterpart = base_map.get(key)
            if counterpart is None:
                added.append(dict(zip(columns, row)))
                continue
            differences = {
                column: {"base": old, "current": new}
                for column, old, new in zip(columns, counterpart, row)
                if old != new
            }
            if differences:
                changed.append({KEY_COLUMN: key, "changes": differences})
        removed = [
            dict(zip(columns, row)) for key, row in base_map.items() if key not in seen
        ]
        return added, changed, removed, ""

    base_counter = Counter(tuple(row) for row in base_rows)
    env_counter = Counter(tuple(row) for row in env_rows)
    added_counter = env_counter - base_counter
    removed_counter = base_counter - env_counter
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


# ---------------- 文件类证据 ----------------


def _collect_file(
    base_dir: Path, env_root: Path, raw: str, now: datetime.datetime | None
) -> dict:
    """采集文本类证据：解析路径占位符，与底座对比出统一 diff。"""
    rel = _resolve_path_placeholders(raw, now)
    return _file_diff(base_dir / rel, env_root / rel, rel)


def _collect_other_changed_files(
    base_dir: Path, env_root: Path, declared: Iterable[str], now: datetime.datetime | None
) -> dict:
    """全树对比底座与环境，收集声明之外被改动的文本文件（含被删的）。"""
    declared_rel = {
        _resolve_path_placeholders(item, now) for item in declared if _is_file_target(item)
    }
    changed: dict[str, dict] = {}
    base_files = _iter_candidate_files(base_dir)
    env_files = _iter_candidate_files(env_root)
    for rel in sorted(set(base_files) | set(env_files)):
        if rel in declared_rel:
            continue
        item = _file_diff(base_dir / rel, env_root / rel, rel)
        if item["changed"]:
            changed[rel] = item
    return changed


def _file_diff(base_path: Path, current_path: Path, rel: str) -> dict:
    """对比同一相对路径在底座与当前环境中的文本，产出统一 diff 结构。"""
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
    base_text = _read_text(base_path)
    if exists and current_text is None:
        result["note"] = "非文本文件，未比对"
        return result

    if not exists:
        # 底座里有、现在没了 = 本次删掉了（也算改动，否则「删了不该删的」会被漏判）
        if base_text is not None:
            result["changed"] = True
            result["deleted"] = True
            result["removed_lines"] = len(base_text.splitlines())
        return result

    result["new_file"] = base_text is None
    before = base_text if base_text is not None else ""
    if before == current_text:
        return result

    diff_lines = list(
        difflib.unified_diff(
            before.splitlines(),
            current_text.splitlines(),
            fromfile="base",
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


def _is_file_target(value: str) -> bool:
    """evidence 取值是「文件」还是「表」：带路径分隔符或以 .md 结尾的按文件处理。"""
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
