"""工具公共基类与错误规范化。

lifeprism 工具（lean 调用）的业务函数返回字符串，错误时以 "Error: " 开头。
在 myagent 框架里，失败应返回 ToolResult.error(...)，以便正确分类（TOOL_EXECUTION）
并被熔断器/评估器识别。这里提供 _result() 做“文本 → ToolResult/str”的归一化，
保持移植工具的输出文本与原 lifeprism 完全一致。
"""

from __future__ import annotations

from typing import Any, Union

from myagent.agent.core.tool.tool import Tool, ToolErrorType, ToolResult

ERROR = "Error: "
SUCCESS = "Success: "


def _result(text: str) -> Union[ToolResult, str]:
    """把工具业务函数返回的字符串归一化为 myagent 结果。

    - 以 "Error: " 开头 -> ToolResult.error(..., TOOL_EXECUTION)
    - 其余           -> 原字符串（成功，由 register 包装为 ToolResult(content=...))
    """
    if isinstance(text, str) and text.startswith(ERROR):
        return ToolResult.error(text, ToolErrorType.TOOL_EXECUTION)
    return text


__all__ = ["ERROR", "SUCCESS", "Tool", "ToolResult", "ToolErrorType", "_result"]