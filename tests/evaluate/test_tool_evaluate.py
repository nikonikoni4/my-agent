"""ToolEvaluate 单元测试：事件订阅、记录提取、CSV 增读、统计与导出。

不经过 agent loop，直接用 EventService 触发 tool/result，验证评估器自身行为；
"走完全部流程"的端到端测试见同目录 test_tool_evaluate_e2e.py。
"""

import csv
import json
from datetime import date, timedelta

import pytest

from myagent.evaluate.tool_evaluate import (
    CSV_COLUMNS,
    CSV_FILE_SUFFIX,
    OverallStats,
    ToolCallRecord,
    ToolEvaluate,
)
from myagent.infra.events.eventspec import TOOL_RESULT
from myagent.infra.events.payload import ToolResultPayload
from myagent.infra.events.service import EventService


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def make_evaluator(output_dir, event_service=None):
    """建一个评估器（默认独立 EventService，便于单独触发）。"""
    event_service = event_service or EventService()
    return ToolEvaluate(event_service, output_dir=output_dir), event_service


def emit(event_service, *, name, arguments="{}", is_error=False, error_type=None, content=""):
    """触发一次 tool/result（字段与 loop 侧填充的负载一致）。"""
    event_service.emit(TOOL_RESULT.name, ToolResultPayload(
        tool_name=name,
        arguments=arguments,
        is_error=is_error,
        error_type=error_type,
        content=content,
    ))


def csv_row(day, name, *, is_error=False, error_type="", argument="{}", content="", duration_ms=""):
    """按模块约定的列格式造一行 CSV 数据（用于构造跨日期固件）。"""
    return {
        "timestamp": f"{day}T10:00:00",
        "date": day,
        "name": name,
        "argument": argument,
        "is_error": str(is_error),
        "error_type": error_type,
        "content": content,
        "duration_ms": "" if duration_ms == "" else str(duration_ms),
    }


def write_day_csv(output_dir, day, rows):
    """直接按约定格式写一天的文件，独立于被测的写入实现（避免自证循环）。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{day}-{CSV_FILE_SUFFIX}"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def read_day_csv(output_dir, day):
    with (output_dir / f"{day}-{CSV_FILE_SUFFIX}").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# 事件订阅与记录提取
# ---------------------------------------------------------------------------


def test_事件触发即提取完整负载并落盘(tmp_path):
    """订阅 tool/result 后：一次事件 = 一条记录 + 追加一行 CSV，字段完整。"""
    evaluator, event_service = make_evaluator(tmp_path)

    emit(
        event_service,
        name="get_weather",
        arguments='{"city": "北京"}',
        is_error=True,
        error_type="param_validation",
        content="status : error\n message : 参数未通过校验",
    )

    day = today_str()
    rows = read_day_csv(tmp_path, day)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "get_weather"
    assert json.loads(row["argument"]) == {"city": "北京"}
    assert row["is_error"] == "True"
    assert row["error_type"] == "param_validation"
    assert row["content"].startswith("status : error")
    assert row["date"] == day and row["timestamp"]


def test_记录以工具名为键聚合(tmp_path):
    """records_by_tool 以工具名为键组织记录（成功与失败都记录）。"""
    evaluator, event_service = make_evaluator(tmp_path)

    emit(event_service, name="a", arguments="{}")
    emit(event_service, name="b", is_error=True, error_type="tool_execution", content="boom")
    emit(event_service, name="a", arguments='{"x": 1}')

    assert set(evaluator.records_by_tool) == {"a", "b"}
    assert len(evaluator.records_by_tool["a"]) == 2
    first = evaluator.records_by_tool["a"][0]
    assert isinstance(first, ToolCallRecord)
    assert first.name == "a"
    assert first.argument == {}
    assert first.is_error is False
    assert first.error_type is None


@pytest.mark.parametrize(
    "raw",
    ['{"q": "x"', "[1, 2]", "null"],  # 坏 JSON / 合法但非对象
    ids=["坏JSON", "数组", "null字面量"],
)
def test_参数解析不出对象时以raw兜底(tmp_path, raw):
    """argument 始终是 JSON 对象：解析失败时以 {"_raw": 原文} 保留原始信息。"""
    evaluator, event_service = make_evaluator(tmp_path)

    emit(event_service, name="t", arguments=raw, is_error=True, error_type="parse_error", content="bad")

    assert evaluator.records_by_tool["t"][0].argument == {"_raw": raw}


def test_成功记录error_type为空(tmp_path):
    """成功调用的 error_type 为 None（is_error=False），content 为工具返回内容。"""
    evaluator, event_service = make_evaluator(tmp_path)

    emit(event_service, name="t", arguments='{"a": 1}', content="ok")

    record = evaluator.records_by_tool["t"][0]
    assert record.is_error is False and record.error_type is None
    assert record.argument == {"a": 1}
    assert record.content == "ok"


# ---------------------------------------------------------------------------
# CSV 文件操作
# ---------------------------------------------------------------------------


def test_csv文件名与表头符合约定(tmp_path):
    evaluator, event_service = make_evaluator(tmp_path)

    emit(event_service, name="t")

    day = today_str()
    path = tmp_path / f"{day}-tool_result_stats.csv"
    assert path.exists(), "文件名必须是 YYYY-MM-DD-tool_result_stats.csv"
    with path.open(encoding="utf-8") as f:
        header = f.readline().strip().split(",")
    assert header == CSV_COLUMNS


def test_同日追加不覆盖_跨实例续写(tmp_path):
    """同一天多次事件累加；新实例（模拟进程重启）继续追加而非覆盖。"""
    evaluator, event_service = make_evaluator(tmp_path)
    emit(event_service, name="t")
    emit(event_service, name="t")

    evaluator2, event_service2 = make_evaluator(tmp_path)
    emit(event_service2, name="t")

    assert len(evaluator2.read_all()) == 3
    with (tmp_path / f"{today_str()}-{CSV_FILE_SUFFIX}").open(encoding="utf-8") as f:
        assert sum(1 for _ in f) == 4, "表头 + 3 行数据"


def test_按日期范围读取_闭区间(tmp_path):
    """read_range 含起止两端，按日期文件名筛选，不读范围外的文件。"""
    today = date.today()
    days = [(today - timedelta(days=2)), (today - timedelta(days=1)), today]
    for index, day in enumerate(days):
        day_str = day.strftime("%Y-%m-%d")
        write_day_csv(tmp_path, day_str, [csv_row(day_str, f"tool{index}", argument='{"i": %d}' % index)])

    evaluator, _ = make_evaluator(tmp_path)
    start = days[1].strftime("%Y-%m-%d")
    mid = days[1].strftime("%Y-%m-%d")
    end = days[2].strftime("%Y-%m-%d")

    assert [r.name for r in evaluator.read_range(start, end)] == ["tool1", "tool2"]
    assert [r.name for r in evaluator.read_range(mid, mid)] == ["tool1"]
    assert [r.name for r in evaluator.read_all()] == ["tool0", "tool1", "tool2"]
    # 从 CSV 读回的 argument 已还原为 JSON 对象
    assert evaluator.read_range(start, end)[0].argument == {"i": 1}


def test_读取时忽略无关文件(tmp_path):
    """目录里的非约定文件名（如统计导出、其他 csv）不参与记录读取。"""
    (tmp_path / "other.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    day = today_str()
    write_day_csv(tmp_path, day, [csv_row(day, "t")])

    evaluator, _ = make_evaluator(tmp_path)

    assert [r.name for r in evaluator.read_all()] == ["t"]
    assert [r.name for r in evaluator.read_range(day, day)] == ["t"]


def test_导出stats到无关文件不污染读取(tmp_path):
    """export_stats 写出的文件名不是 YYYY-MM-DD-... 约定名，re-index 时不参与。"""
    evaluator, event_service = make_evaluator(tmp_path)
    emit(event_service, name="t")
    evaluator.export_stats(evaluator.stats(), tmp_path / "tool_result_stats_summary.csv")

    assert [r.name for r in evaluator.read_all()] == ["t"]


# ---------------------------------------------------------------------------
# 统计分析
# ---------------------------------------------------------------------------


def test_统计_每工具与整体正确率及错误类型分布(tmp_path):
    evaluator, event_service = make_evaluator(tmp_path)
    emit(event_service, name="a", arguments='{"x": 1}', content="ok")
    emit(event_service, name="a", is_error=True, error_type="parse_error", content="e1")
    emit(event_service, name="a", is_error=True, error_type="parse_error", content="e2")
    emit(event_service, name="b", is_error=True, error_type="tool_execution", content="e3")

    stats = evaluator.stats()

    assert (stats.total, stats.success, stats.failure) == (4, 1, 3)
    assert stats.accuracy == pytest.approx(0.25)
    assert stats.success_rate == pytest.approx(0.25)
    assert stats.failure_rate == pytest.approx(0.75)

    a = next(t for t in stats.tools if t.name == "a")
    b = next(t for t in stats.tools if t.name == "b")
    assert (a.total, a.success, a.failure) == (3, 1, 2)
    assert a.success_rate == pytest.approx(1 / 3)
    assert a.error_type_distribution == {"parse_error": 2}
    assert (b.total, b.success, b.failure) == (1, 0, 1)
    assert b.error_type_distribution == {"tool_execution": 1}


def test_统计限定日期范围只看范围内的工具(tmp_path):
    today = date.today()
    yesterday = today - timedelta(days=1)
    write_day_csv(
        tmp_path,
        yesterday.strftime("%Y-%m-%d"),
        [csv_row(yesterday.strftime("%Y-%m-%d"), "old", is_error=True, error_type="tool_execution", content="boom")],
    )

    evaluator, event_service = make_evaluator(tmp_path)
    emit(event_service, name="new", content="ok")

    today_str_ = today.strftime("%Y-%m-%d")
    stats = evaluator.stats(today_str_, today_str_)

    assert stats.total == 1
    assert [t.name for t in stats.tools] == ["new"]
    assert all("tool_execution" not in t.error_type_distribution for t in stats.tools)


def test_无记录时统计与报告不报错(tmp_path):
    evaluator, _ = make_evaluator(tmp_path)

    stats = evaluator.stats()

    assert isinstance(stats, OverallStats)
    assert (stats.total, stats.success, stats.failure) == (0, 0, 0)
    assert stats.accuracy == 0.0
    report = evaluator.format_report(stats)
    assert "总调用次数 : 0" in report
    assert "（无记录）" in report


def test_报告渲染含工具明细与错误类型分布(tmp_path):
    evaluator, event_service = make_evaluator(tmp_path)
    emit(event_service, name="search", is_error=True, error_type="parse_error", content="e")
    emit(event_service, name="search", content="ok")

    report = evaluator.format_report(evaluator.stats())

    assert "[search]" in report
    assert "正确率 50.0%" in report
    assert "错误类型分布：parse_error:1" in report


def test_导出统计CSV含整体汇总行(tmp_path):
    evaluator, event_service = make_evaluator(tmp_path)
    emit(event_service, name="a", content="ok")
    emit(event_service, name="a", is_error=True, error_type="tool_execution", content="e")

    out = evaluator.export_stats(evaluator.stats(), tmp_path / "report" / "stats.csv")

    assert out.exists()
    with out.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["name"] for r in rows] == ["a", "__overall__"]
    assert (rows[0]["total"], rows[0]["success"], rows[0]["failure"]) == ("2", "1", "1")
    assert rows[0]["success_rate"] == "50.0%"
    assert json.loads(rows[0]["error_type_distribution"]) == {"tool_execution": 1}
    assert rows[1]["total"] == "2" and rows[1]["error_type_distribution"] == ""
