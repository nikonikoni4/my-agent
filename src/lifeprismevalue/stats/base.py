"""统计组件基类。

解耦约定：每种统计类型是一个独立组件，统一以一次 SessionView 为输入，
输出可 JSON 序列化的 dict；各组件互不依赖，由 Runner 遍历执行后合并输出。
新增统计维度 = 新增一个组件并注册，不改动已有组件。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from lifeprismevalue.stats.session_view import SessionView


class StatsComponent(ABC):
    """统计组件基类。"""

    #: 组件名，同时作为合并结果里的键；子类必须覆写
    name: str = "component"

    @abstractmethod
    def analyze(self, session: SessionView) -> dict[str, Any]:
        """读取一次 session，返回本维度的统计结果（可 JSON 序列化）。"""

    def render(self, result: dict[str, Any]) -> str:
        """把本组件结果渲染为可读文本；默认退化为 JSON，组件可覆写。"""
        return json.dumps(result, ensure_ascii=False, indent=2)
