"""提示词版本库（lifeprismevalue）。

由 lifeprism 的 `llm/prompts/prompt_loader.py` 迁移而来：把提示词按版本存进
`lifeprismData/prompts/agent_prompts.yaml`，加载时按 `active_version`（或显式指定
版本）取正文，版本切换只改数据文件、不动代码。

用量入口见 `loader.PromptLoader`，文件格式见 `loader` 模块的文档字符串。
"""

from __future__ import annotations

from lifeprismevalue.prompts.loader import (
    DEFAULT_PROMPTS_REL_PATH,
    PromptLoader,
    default_prompts_path,
    split_sections,
)

__all__ = [
    "DEFAULT_PROMPTS_REL_PATH",
    "PromptLoader",
    "default_prompts_path",
    "split_sections",
]
