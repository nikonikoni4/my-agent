"""提示词版本库加载（agent_prompts.yaml）。

迁自 lifeprism 的 `llm/prompts/prompt_loader.py`。原设计用一个文件承载同一批提示词
的多个版本，靠 `active_version` 选中当前版本、靠 `version_history` 记录每版为何而改
——版本切换只改数据文件，不动代码。本模块保留这套设计，只按本仓库的情况做了三处简化：

1. **文件是 yaml**（`prompt.metadata` + `prompt.versions`），不再解析旧格式里
   md 的 `# 名称` 与 `## metadata` 代码块。
2. **一个版本正文内含多段**，段之间用一级标题分隔（`# identity`、`# soul`…）。
   `load_segments` 按一级标题切段，段名即代码里注册 section 用的名字。
3. **不做参数注入**：`{agent_path}` 这类占位符留在正文里，由装配层在组装请求时
   渲染（见 `ReActAgentLoop` 的 `prompt_render_parame`）。
   也**不做使用统计**：旧实现每次 load 都写一次 usage_stats.yaml，而 chat 是每步
   都组装 system prompt，会退化成每步一次磁盘写。

文件格式：

    prompt:
      metadata:
        active_version: v1
        version_history:
          v0:
            created_at: '2026-09-15'
            change_reason: 迁移前的旧提示词
          v1:
            created_at: '2026-09-15'
            change_reason: 当前提示词
      versions:
        v0: |
          # identity
          ...
          # soul
          ...
        v1: |
          # identity
          ...
    tools:   # 工具版本记录：版本名 -> [{rev, note}]
      v1:
        - rev: 7c2aba8
          note: 首次登记
    react:   # react agent 版本记录，结构同 tools
      v1:
        - rev: 7c2aba8
          note: 首次登记

约定：一个版本正文必须从一级标题开始（段标题），正文中不再出现别的一级标题，
否则会被当成新段的开始。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from lifeprismevalue.config import get_lifeprism_data_path

# agent_prompts.yaml 相对 lifeprism 数据根的路径
DEFAULT_PROMPTS_REL_PATH = "prompts/agent_prompts.yaml"

# 段标记：行首的一级标题（`# 段名`）
_SECTION_PATTERN = re.compile(r"^#\s+(?P<name>\S.*)$")


def default_prompts_path() -> Path:
    """agent_prompts.yaml 的默认路径（跟随 LIFEPRISMEVALUE_DATA_PATH）。"""
    return get_lifeprism_data_path() / DEFAULT_PROMPTS_REL_PATH


def split_sections(text: str) -> dict[str, str]:
    """按一级标题把一个版本正文切成 {段名: 段文本}。

    段文本**含**它的一级标题行——该标题本身就是要发给模型的内容的一部分。

    Args:
        text: 一个版本的整篇正文。

    Returns:
        段名 -> 段文本，顺序与正文中出现的顺序一致。

    Raises:
        ValueError: 首个一级标题之前出现非空内容，或段名重复（重复即无法区分，
            属作者笔误，不静默覆盖）。
    """
    sections: dict[str, str] = {}
    name: str | None = None
    buffer: list[str] = []

    for line in text.splitlines():
        matched = _SECTION_PATTERN.match(line)
        if matched is not None:
            _add_section(sections, name, buffer)
            name = matched.group("name").strip()
            buffer = [line]
            continue
        if name is None:
            # 首个一级标题之前只允许空行
            if line.strip():
                raise ValueError(f"版本正文必须从一级标题开始，但遇到：{line!r}")
            continue
        buffer.append(line)

    _add_section(sections, name, buffer)
    return sections


def _add_section(sections: dict[str, str], name: str | None, buffer: list[str]) -> None:
    """把一段收进结果；name 为 None 表示还没遇到第一个标题，直接跳过。"""
    if name is None:
        return
    if name in sections:
        raise ValueError(f"段名重复：{name}")
    # 只去掉段尾多余的空行（块标量带来的换行），段内空行原样保留
    sections[name] = "\n".join(buffer).rstrip("\n")


class PromptLoader:
    """提示词版本库：按版本读取 agent 提示词。

    解析结果按实例缓存，文件被改动后调用 `reload()` 重新读取。测试与评测循环里
    通常在每次 run 开始处 reload 一次。

    Example:
        loader = PromptLoader()
        segments = loader.load_segments()          # 取 active_version 的各段
        segments_v0 = loader.load_segments("v0")   # 指定版本
    """

    def __init__(self, path: str | Path | None = None) -> None:
        """
        Args:
            path: agent_prompts.yaml 路径；为空时取 `default_prompts_path()`。
        """
        self.path = Path(path) if path is not None else default_prompts_path()
        self._document: dict[str, Any] | None = None

    @classmethod
    def for_data_path(cls, data_path: str | Path) -> "PromptLoader":
        """按 lifeprism 数据根定位版本库：`<data_path>/prompts/agent_prompts.yaml`。

        评测时数据根是本次 run 的工作副本（不是默认的 lifeprismData），故按数据根
        定位而不是取默认路径。

        Args:
            data_path: lifeprism 数据根目录。

        Returns:
            指向该数据根下版本库的 loader。
        """
        return cls(Path(data_path) / DEFAULT_PROMPTS_REL_PATH)

    def reload(self) -> None:
        """清空缓存，下次访问重新读文件。"""
        self._document = None

    # ---------- 元数据 ----------

    @property
    def active_version(self) -> str:
        """当前生效的版本名（`prompt.metadata.active_version`）。"""
        metadata = self._prompt_block().get("metadata")
        if not isinstance(metadata, dict) or not metadata.get("active_version"):
            raise ValueError(f"{self.path} 缺少 prompt.metadata.active_version")
        return str(metadata["active_version"])

    def available_versions(self) -> list[str]:
        """文件里已有的全部版本名，顺序与文件中一致。"""
        return list(self._versions())

    def metadata(self) -> dict[str, Any]:
        """返回 `prompt.metadata` 的浅拷贝（active_version + version_history）。"""
        metadata = self._prompt_block().get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError(f"{self.path} 缺少 prompt.metadata")
        return dict(metadata)

    # ---------- 取正文 ----------

    def load(self, version: str | None = None) -> str:
        """取指定版本的整篇正文（含各段的一级标题）。

        Args:
            version: 版本名；为空时取 `active_version`。

        Returns:
            该版本的正文文本。

        Raises:
            ValueError: 版本名不存在。
        """
        return str(self._versions()[self._resolve(version)])

    def load_segments(self, version: str | None = None) -> dict[str, str]:
        """取指定版本并按一级标题切段，返回 {段名: 段文本}。

        Args:
            version: 版本名；为空时取 `active_version`。

        Returns:
            段名 -> 段文本，顺序与正文中一致。

        Raises:
            ValueError: 版本名不存在，或正文不符合「从一级标题开始」的约定。
        """
        return split_sections(self.load(version))

    # ---------- 内部 ----------

    def _resolve(self, version: str | None) -> str:
        """把 None 解析为 active_version，并校验版本存在。"""
        versions = self._versions()
        target = version if version is not None else self.active_version
        if target not in versions:
            raise ValueError(f"版本 {target!r} 不存在于 {self.path}，可用版本：{list(versions)}")
        return target

    def _prompt_block(self) -> dict[str, Any]:
        prompt = self._document_root().get("prompt")
        if not isinstance(prompt, dict):
            raise ValueError(f"{self.path} 缺少 prompt 段")
        return prompt

    def _versions(self) -> dict[str, Any]:
        versions = self._prompt_block().get("versions")
        if not isinstance(versions, dict) or not versions:
            raise ValueError(f"{self.path} 的 prompt.versions 为空或格式不对")
        return versions

    def _document_root(self) -> dict[str, Any]:
        """读取并缓存整个文件（首次访问时读盘）。"""
        if self._document is not None:
            return self._document
        if not self.path.is_file():
            raise FileNotFoundError(f"提示词文件不存在：{self.path}")
        document = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"{self.path} 顶层必须是 mapping")
        self._document = document
        return document
