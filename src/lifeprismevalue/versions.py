"""版本记录：把「本次运行用了哪一版」显式化。

三个版本轴（与 `lifeprismData/prompts/agent_prompts.yaml` 的顶层 key 对应）：

- `prompt`：提示词。版本名来自版本库的 `active_version`——装配层就是按它取段的，
  所以记下来的即实际生效的那一版（见 `lifeprismevalue.prompts.PromptLoader`）。
- `tools` ：工具。版本名由 **git rev 反查登记表**得到：登记表里 `name -> [{rev, note}]`，
  运行时取实际 HEAD，命中哪条就属于哪一版。
- `react` ：ReAct agent 框架。同 tools。

为什么代码类只做「反查」而不是「运行时按版本加载」：代码在 git 里，版本即 commit，
回滚靠 `git checkout <rev>`；运行时能做且必须做的是**把当前实际跑的代码对上是哪一版**，
并校验 rev 是否可信（工作区脏时 HEAD 会撒谎）。这样"切了版本却没生效"和"记错版本"
都不会静默发生。

prompt 与代码类的差别来自介质：提示词不在 git 里（`lifeprismData` 被 gitignore），
没有 rev 可用，所以它必须自带内容与版本名；代码有 git，版本名可以算出来。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from lifeprismevalue.prompts import default_prompts_path

# 三个版本轴（同 agent_prompts.yaml 的顶层 key）
AXIS_PROMPT = "prompt"
AXIS_TOOLS = "tools"
AXIS_REACT = "react"

# 反查不到版本时的占位名（显式暴露"当前代码未登记"，而不是静默留空）
UNREGISTERED = "unregistered"

# 各版本轴覆盖的源码范围（相对仓库根）。工具的实现不只在 tools/，
# 还依赖 data/ 层，故一并纳入——否则 data 改了会出现"tools 没变但工具行为变了"。
AXIS_PATHS: dict[str, tuple[str, ...]] = {
    AXIS_TOOLS: ("src/lifeprismevalue/tools/", "src/lifeprismevalue/data/"),
    AXIS_REACT: ("src/myagent/",),
}

# agent 项目根：本文件在 src/lifeprismevalue/ 下，往上两级
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class CodeVersion:
    """一个代码版本轴的解析结果。

    Attributes:
        axis: 版本轴（tools / react）。
        version: 反查登记表得到的版本名；未命中时为 `UNREGISTERED`。
        rev: 登记表里那一条的 rev（未命中为空）。
        head: 实际仓库 HEAD 的短 rev。
        dirty: 该轴覆盖的路径是否存在未提交改动——脏时 HEAD 不能代表实际代码。
        dirty_files: 上述未提交改动的文件清单（`git status --porcelain`）。
        note: 登记表里那一条的备注。
        error: 解析过程中的问题（非 git 仓库 / 版本库缺失 / 未登记等），空表示一切正常。
    """

    axis: str
    version: str
    rev: str = ""
    head: str = ""
    dirty: bool = False
    dirty_files: tuple[str, ...] = ()
    note: str = ""
    error: str = ""

    @property
    def trusted(self) -> bool:
        """这个版本名是否可信（已登记、rev 命中且工作区干净）。"""
        return self.version != UNREGISTERED and not self.dirty and not self.error

    def to_dict(self) -> dict[str, Any]:
        """转成落盘用的 dict（run.json / summary 用）。"""
        return {
            "version": self.version,
            "rev": self.rev,
            "head": self.head,
            "dirty": self.dirty,
            "dirty_files": list(self.dirty_files),
            "note": self.note,
            "error": self.error,
        }


@dataclass
class VersionBook:
    """读版本库并解析三个版本轴的当前取值。

    版本库缺失或格式非法不抛错，转成各轴的 `error` 字段——评测里记不上版本
    不应把整次 run 打挂。

    Attributes:
        book_path: agent_prompts.yaml 路径。
        repo_root: 仓库根目录（跑 git 命令用）。
    """

    book_path: Path = field(default_factory=default_prompts_path)
    repo_root: Path = field(default_factory=lambda: _PROJECT_ROOT)

    def __post_init__(self) -> None:
        # 版本库的解析缓存（首次访问才读盘）
        self._document: dict[str, Any] | None = None

    @classmethod
    def for_data_path(cls, data_path: str | Path, *, repo_root: str | Path | None = None) -> "VersionBook":
        """按 lifeprism 数据根定位版本库（评测时数据根是本次 run 的工作副本）。"""
        return cls(
            book_path=Path(data_path) / "prompts" / "agent_prompts.yaml",
            repo_root=Path(repo_root) if repo_root is not None else _PROJECT_ROOT,
        )

    def reload(self) -> None:
        """清空缓存，下次访问重新读文件。"""
        self._document = None

    # ---------- 三个版本轴 ----------

    def prompt_version(self) -> str:
        """提示词版本名（版本库的 active_version）；取不到返回空串。"""
        try:
            prompt = self._read_root().get(AXIS_PROMPT)
            if isinstance(prompt, dict):
                metadata = prompt.get("metadata")
                if isinstance(metadata, dict) and metadata.get("active_version"):
                    return str(metadata["active_version"])
        except (OSError, ValueError):
            pass
        return ""

    def code_version(self, axis: str) -> CodeVersion:
        """解析一个代码版本轴：取 HEAD → 反查登记表得到版本名 → 校验工作区脏否。

        Args:
            axis: 版本轴名（`tools` / `react`）。

        Returns:
            该轴的解析结果；任何异常都收敛为 `error` 字段，不抛出。
        """
        head_full, error = self._head()
        head = self._head_short() or head_full[:7]
        dirty, dirty_files = self._dirty_state(axis)

        if error:
            return CodeVersion(
                axis=axis,
                version=UNREGISTERED,
                head=head,
                dirty=dirty,
                dirty_files=dirty_files,
                error=error,
            )

        entries, book_error = self._axis_entries(axis)
        for version, entry in entries:
            registered = str(entry.get("rev", "")).strip()
            # 登记的 rev 可能短于 HEAD，也可能因 rebase/amend 已指向不存在的对象；
            # 用前缀比对即可覆盖短 rev，重写历史导致的不匹配会显式落成 unregistered
            if registered and head_full.startswith(registered):
                return CodeVersion(
                    axis=axis,
                    version=version,
                    rev=registered,
                    head=head,
                    dirty=dirty,
                    dirty_files=dirty_files,
                    note=str(entry.get("note", "")),
                    error=book_error,
                )
        return CodeVersion(
            axis=axis,
            version=UNREGISTERED,
            head=head,
            dirty=dirty,
            dirty_files=dirty_files,
            error=book_error or f"当前 HEAD({head}) 未在 {axis} 登记表中",
        )

    def snapshot(self) -> dict[str, Any]:
        """三个版本轴的完整快照（写 run.json / summary 用）。

        Returns:
            形如 `{"prompt": {"version": "v1"}, "tools": {...}, "react": {...}}`；
            prompt 取不到版本名时 `version` 为空串。
        """
        return {
            AXIS_PROMPT: {"version": self.prompt_version()},
            AXIS_TOOLS: self.code_version(AXIS_TOOLS).to_dict(),
            AXIS_REACT: self.code_version(AXIS_REACT).to_dict(),
        }

    # ---------- 内部：版本库 ----------

    def _read_root(self) -> dict[str, Any]:
        """读取并缓存版本库根 mapping。"""
        if self._document is not None:
            return self._document
        document = yaml.safe_load(self.book_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"{self.book_path} 顶层必须是 mapping")
        self._document = document
        return document

    def _axis_entries(self, axis: str) -> tuple[list[tuple[str, dict]], str]:
        """取某个轴的登记项，展开为 [(版本名, 条目), ...]。

        Returns:
            (登记项列表, 错误信息)；版本库缺失时返回空列表 + 错误信息。
        """
        try:
            section = self._read_root().get(axis)
        except (OSError, ValueError) as exception:
            return [], f"版本库读取失败：{exception}"
        if not isinstance(section, dict):
            return [], f"版本库缺少 {axis} 段"

        entries: list[tuple[str, dict]] = []
        for version, items in section.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    entries.append((str(version), item))
        if not entries:
            return [], f"版本库的 {axis} 段没有登记项"
        return entries, ""

    # ---------- 内部：git ----------

    def _head(self) -> tuple[str, str]:
        """取完整 HEAD。Returns: (head, 错误信息)。"""
        ok, output = _git(self.repo_root, "rev-parse", "HEAD")
        if not ok:
            return "", f"取 HEAD 失败：{output}"
        return output, ""

    def _head_short(self) -> str:
        """取短 HEAD（拿不到则返回空串，调用方用完整 rev 截断兜底）。"""
        ok, output = _git(self.repo_root, "rev-parse", "--short", "HEAD")
        return output if ok else ""

    def _dirty_state(self, axis: str) -> tuple[bool, tuple[str, ...]]:
        """该轴覆盖路径下的未提交改动。Returns: (是否有改动, 文件清单)。"""
        paths = AXIS_PATHS.get(axis, ())
        if not paths:
            return False, ()
        ok, output = _git(self.repo_root, "status", "--porcelain", "--", *paths)
        if not ok:
            return False, ()
        # porcelain 格式：XY<空格>路径，故取第 3 个字符起
        files = tuple(line[3:].strip() for line in output.splitlines() if line.strip())
        return bool(files), files


def _git(repo_root: Path, *args: str) -> tuple[bool, str]:
    """在 repo_root 下跑一条 git 命令。

    Args:
        repo_root: 仓库根目录。
        args: git 子命令及参数。

    Returns:
        (是否成功, 标准输出或错误信息)。git 不存在、非 git 仓库等一律返回
        (False, 原因)，不抛异常。
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exception:
        return False, str(exception)
    if completed.returncode != 0:
        return False, (completed.stderr or completed.stdout).strip()
    return True, completed.stdout.strip()
