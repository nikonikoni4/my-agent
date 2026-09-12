"""工具调用评估：订阅 `tool/result` 事件，记录并统计每次工具调用的准确性。

职责边界：
- 只读订阅。评估器不参与 agent 决策，回调内只做记录，不改变循环行为。
- CSV 是唯一数据源。回调即时把记录追加到按日期命名的 CSV，统计一律读盘，
  避免"内存一份、磁盘一份"两份状态随时间漂移。
- 以工具名为键聚合。`records_by_tool` 按工具组织记录，便于定位"哪个工具
  老出错、错在哪一类"（错误分类即 ToolErrorType 的值）。

生命周期提醒：EventService 以弱引用（WeakMethod）持有回调，调用方必须自己
持有 ToolEvaluate 实例，否则订阅在实例被回收后立即失效。
"""

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from myagent.config.system_config import local_data_path
from myagent.infra.events.eventspec import TOOL_RESULT
from myagent.infra.events.payload import ToolResultPayload

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CSV_FILE_SUFFIX = "tool_result_stats.csv"  # 文件名为 YYYY-MM-DD-tool_result_stats.csv
CSV_COLUMNS = ["timestamp", "date", "name", "argument", "is_error", "error_type", "content"]
DATE_FORMAT = "%Y-%m-%d"
UNKNOWN_ERROR_TYPE = "unknown"  # 失败但未带分类时（历史数据/未分类路径）的兜底键


@dataclass
class ToolCallRecord:
    """单次工具调用的评估记录。

    argument 为 JSON 对象；解析不出对象时以 {"_raw": 原文} 兜底，
    保证字段类型稳定且不丢原始信息。
    """

    timestamp: str            # 结果产生时间（isoformat）
    date: str                 # 归属日期（YYYY-MM-DD），决定写入哪个 CSV
    name: str                 # 工具名称（非空）
    argument: dict[str, Any]  # 工具调用参数（JSON 对象）
    is_error: bool            # True=调用失败，False=调用成功
    error_type: str | None    # 失败分类（ToolErrorType 的值），成功为 None
    content: str              # 回喂给模型的内容（失败时为错误详情）

    def to_row(self) -> dict[str, str]:
        """转为 CSV 行（全部字段按字符串序列化）。"""
        return {
            "timestamp": self.timestamp,
            "date": self.date,
            "name": self.name,
            "argument": json.dumps(self.argument, ensure_ascii=False),
            "is_error": str(self.is_error),
            "error_type": self.error_type or "",
            "content": self.content,
        }

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "ToolCallRecord":
        """从 CSV 行还原记录。"""
        raw_argument = row.get("argument", "")
        return cls(
            timestamp=row.get("timestamp", ""),
            date=row.get("date", ""),
            name=row.get("name", ""),
            argument=json.loads(raw_argument) if raw_argument else {},
            is_error=row.get("is_error", "").strip().lower() == "true",
            error_type=row.get("error_type") or None,
            content=row.get("content", ""),
        )


@dataclass
class ToolStats:
    """单个工具的调用统计。"""

    name: str
    total: int = 0
    success: int = 0
    failure: int = 0
    error_type_distribution: dict[str, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        """该工具单独正确率 = 成功次数 / 总调用次数。"""
        return self.success / self.total if self.total else 0.0

    @property
    def failure_rate(self) -> float:
        """该工具失败占比 = 失败次数 / 总调用次数。"""
        return self.failure / self.total if self.total else 0.0


@dataclass
class OverallStats:
    """整体调用统计（tools 为各工具明细）。"""

    total: int = 0
    success: int = 0
    failure: int = 0
    tools: list[ToolStats] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        """总正确率 = 成功调用次数 / 总调用次数。"""
        return self.success / self.total if self.total else 0.0

    @property
    def success_rate(self) -> float:
        """总体调用成功占比。"""
        return self.accuracy

    @property
    def failure_rate(self) -> float:
        """总体调用失败占比。"""
        return self.failure / self.total if self.total else 0.0


class ToolEvaluate:
    """工具调用评估器。

    构造时订阅 `tool/result`，事件触发即记录一次工具调用结果；对外提供
    CSV 追加/按日期范围读取、工具维度与整体维度统计、报告渲染与导出。
    """

    def __init__(self, event_service, output_dir: str | Path | None = None):
        """
        Args:
            event_service: 事件服务，评估器在其上订阅 `tool/result`。
            output_dir: 评估 CSV 的输出目录，默认 ./localData/evaluate。
        """
        self._event_service = event_service
        self._output_dir = Path(output_dir) if output_dir else local_data_path / "evaluate"
        # 内存中按工具名聚合本次进程已收到的记录：{工具名: [记录...]}
        self._records: dict[str, list[ToolCallRecord]] = {}
        # 订阅 tool/result（事件名为字符串，回调约定只接收 payload 一个参数）
        self._event_service.register(TOOL_RESULT.name, self._on_tool_result)

    # ---------- 事件订阅 ----------

    def _on_tool_result(self, payload: ToolResultPayload) -> None:
        """`tool/result` 回调：从事件负载提取完整调用结果并记录。"""
        current = datetime.now()
        record = ToolCallRecord(
            timestamp=current.isoformat(),
            date=current.strftime(DATE_FORMAT),
            name=payload.tool_name,
            argument=self._parse_arguments(payload.arguments),
            is_error=payload.is_error,
            error_type=payload.error_type,
            content=payload.content,
        )
        self._records.setdefault(record.name, []).append(record)
        self._append_record(record)

    @property
    def records_by_tool(self) -> dict[str, list[ToolCallRecord]]:
        """本次进程内已收集的记录，以工具名为键。"""
        return self._records

    @staticmethod
    def _parse_arguments(raw: str) -> dict[str, Any]:
        """把 wire 上的 JSON 字符串参数解析为 JSON 对象。

        解析失败或结果非对象时以 {"_raw": 原文} 兜底（参数本身可能是坏
        JSON，此时原文对定位问题更有价值）。
        """
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {"_raw": raw}
        return parsed if isinstance(parsed, dict) else {"_raw": raw}

    # ---------- CSV 文件操作 ----------

    def _csv_path(self, day: str) -> Path:
        """按日期取 CSV 路径：YYYY-MM-DD-tool_result_stats.csv。"""
        return self._output_dir / f"{day}-{CSV_FILE_SUFFIX}"

    def _append_record(self, record: ToolCallRecord) -> None:
        """把一条记录追加到归属日期的 CSV（文件不存在时先写表头）。"""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        path = self._csv_path(record.date)
        write_header = not path.exists()
        # 追加模式 + 存在性判断，保证同日多批数据累加而非覆盖
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerow(record.to_row())

    def read_range(self, start_date: str, end_date: str) -> list[ToolCallRecord]:
        """读取 [start_date, end_date] 闭区间内所有工具调用记录。

        Args:
            start_date: 起始日期，YYYY-MM-DD。
            end_date: 结束日期（含当天），YYYY-MM-DD。
        """
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        records: list[ToolCallRecord] = []
        for path in sorted(self._output_dir.glob(f"*-{CSV_FILE_SUFFIX}")):
            day = path.name[: -len(f"-{CSV_FILE_SUFFIX}")]
            try:
                day_date = date.fromisoformat(day)
            except ValueError:
                continue  # 命名不符合本模块格式，忽略
            if start <= day_date <= end:
                records.extend(self._read_csv(path))
        return records

    def read_all(self) -> list[ToolCallRecord]:
        """读取输出目录下全部已落盘记录。"""
        records: list[ToolCallRecord] = []
        for path in sorted(self._output_dir.glob(f"*-{CSV_FILE_SUFFIX}")):
            records.extend(self._read_csv(path))
        return records

    @staticmethod
    def _read_csv(path: Path) -> list[ToolCallRecord]:
        with path.open("r", newline="", encoding="utf-8") as f:
            return [ToolCallRecord.from_row(row) for row in csv.DictReader(f)]

    # ---------- 统计分析 ----------

    def stats(self, start_date: str | None = None, end_date: str | None = None) -> OverallStats:
        """统计工具调用情况。

        Args:
            start_date: 起始日期，YYYY-MM-DD；与 end_date 同时给出时按范围统计。
            end_date: 结束日期（含当天），YYYY-MM-DD。
        """
        if start_date and end_date:
            records = self.read_range(start_date, end_date)
        else:
            records = self.read_all()
        return self._aggregate(records)

    @staticmethod
    def _aggregate(records: list[ToolCallRecord]) -> OverallStats:
        """把记录聚合成整体统计 + 各工具统计（按调用量降序）。"""
        per_tool: dict[str, ToolStats] = {}
        overall = OverallStats()
        for record in records:
            tool = per_tool.setdefault(record.name, ToolStats(name=record.name))
            tool.total += 1
            overall.total += 1
            if record.is_error:
                tool.failure += 1
                overall.failure += 1
                key = record.error_type or UNKNOWN_ERROR_TYPE
                tool.error_type_distribution[key] = tool.error_type_distribution.get(key, 0) + 1
            else:
                tool.success += 1
                overall.success += 1
        overall.tools = sorted(per_tool.values(), key=lambda t: t.total, reverse=True)
        return overall

    @staticmethod
    def _pct(value: float) -> str:
        return f"{value * 100:.1f}%"

    def format_report(self, stats: OverallStats) -> str:
        """把统计结果渲染为可读文本报告。"""
        lines = [
            "===== 工具调用评估报告 =====",
            f"总调用次数 : {stats.total}",
            f"成功次数   : {stats.success}（{self._pct(stats.success_rate)}）",
            f"失败次数   : {stats.failure}（{self._pct(stats.failure_rate)}）",
            f"总正确率   : {self._pct(stats.accuracy)}",
            "",
            "-- 各工具明细 --",
        ]
        if not stats.tools:
            lines.append("（无记录）")
        for tool in stats.tools:
            lines.append(
                f"[{tool.name}] 总 {tool.total} | 成功 {tool.success} | 失败 {tool.failure}"
                f" | 正确率 {self._pct(tool.success_rate)}"
            )
            if tool.error_type_distribution:
                dist = "，".join(
                    f"{etype}:{count}"
                    for etype, count in sorted(
                        tool.error_type_distribution.items(), key=lambda kv: kv[1], reverse=True
                    )
                )
                lines.append(f"    错误类型分布：{dist}")
        return "\n".join(lines)

    def export_stats(self, stats: OverallStats, path: str | Path) -> Path:
        """把统计结果导出为 CSV（各工具一行，末行为整体汇总）。

        结构化导出便于二次加工与可视化；如需 JSON，可直接 asdict(stats)。
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = ["name", "total", "success", "failure", "success_rate", "failure_rate", "error_type_distribution"]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            for tool in stats.tools:
                writer.writerow({
                    "name": tool.name,
                    "total": tool.total,
                    "success": tool.success,
                    "failure": tool.failure,
                    "success_rate": self._pct(tool.success_rate),
                    "failure_rate": self._pct(tool.failure_rate),
                    "error_type_distribution": json.dumps(tool.error_type_distribution, ensure_ascii=False),
                })
            writer.writerow({
                "name": "__overall__",
                "total": stats.total,
                "success": stats.success,
                "failure": stats.failure,
                "success_rate": self._pct(stats.success_rate),
                "failure_rate": self._pct(stats.failure_rate),
                "error_type_distribution": "",
            })
        return path
