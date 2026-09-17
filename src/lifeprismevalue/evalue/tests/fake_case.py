"""测试用的假用例执行实体：**子进程里真正跑起来的那一个**。

放在包内（而不是测试文件里）是因为子进程要用 `"module.path:function"` 把它 import
回来——测试文件的模块名不在这个约定里。它只跟评测内核打交道（读 payload、写环境、
返回 `CaseResult` 形状的字段），不碰 myagent、不调 LLM，所以整条「runner → EvalCore →
子进程 → 领域层」的链路可以离线跑。

行为由**用例 id 的前缀**决定（cases.yaml 由测试自己写，故这是可控的约定）：

| id 前缀 | 行为 | 用来验什么 |
| --- | --- | --- |
| `fail` | 抛异常 | 执行通道失败 → 记 error、保留现场 |
| `crash` | `os._exit(3)`（不写结果文件） | 子进程非零退出 / 没产出结果 |
| `observe` | 先读上一条用例留下的标记，再写自己的 | 槽位复用前必须整份回滚（读到的必须是空） |
| `sleep` | 睡 0.3s，把起止时间写进 `case_dir/events.json` | 槽位真的并行（时间区间重叠） |
| 其它 | 正常返回 | 同序、产物、summary |
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import yaml

# 环境里用来留下「被写过」痕迹的文件名（宿主可直接读，故也是 environment 可见性的证据）
MARKER = "marker.txt"

# sleep 行为的固定时长（秒）：要比「并发子进程之间的启动抖动」明显长一截，
# 否则「两个槽位的区间是否重叠」会被启动快慢影响（见 test_runner 的并行用例）。
SLEEP_SECONDS = 0.8


def execute(payload: dict, env: Any) -> dict:
    """假用例的入口（`fn(payload, env) -> dict`）。"""
    case_id, case_type = _case_meta(payload)
    case_dir = Path(payload["case_dir"])
    case_dir.mkdir(parents=True, exist_ok=True)

    if case_id.startswith("fail"):
        raise RuntimeError("故意失败")
    if case_id.startswith("crash"):
        os._exit(3)  # 模拟子进程被杀 / 崩掉：不写结果文件

    if case_id.startswith("observe"):
        marker = env.root / MARKER
        observed = marker.read_text(encoding="utf-8") if marker.exists() else ""
        marker.write_text(f"被用例 {payload['index']} 写过", encoding="utf-8")
        _write(case_dir / "observed.json", {"observed": observed})
        return _result(payload, case_id, case_type, passed=True, reason=f"observed={observed!r}")

    if case_id.startswith("sleep"):
        started = time.time()
        time.sleep(SLEEP_SECONDS)
        _write(case_dir / "events.json", {"start": started, "end": time.time()})
        return _result(payload, case_id, case_type, passed=True, reason="sleep")

    # 默认行为：写环境、写用例快照，返回一份「通过」的结果
    (env.root / MARKER).write_text("写过", encoding="utf-8")
    _write(case_dir / "case.yaml", {"id": case_id, "type": case_type})
    return _result(payload, case_id, case_type, passed=bool(payload.get("passed", True)))


def _case_meta(payload: dict) -> tuple[str, str]:
    """从 cases.yaml 里取这条用例的 id 与 type（子进程侧看不见内存对象，只能读文件）。"""
    raw = yaml.safe_load(Path(payload["cases_path"]).read_text(encoding="utf-8"))
    case = raw["cases"][int(payload["index"])]
    return str(case["id"]), str(case.get("type") or "")


def _result(
    payload: dict, case_id: str, case_type: str, *, passed: bool | None, reason: str = ""
) -> dict:
    """`case.py` 那套结果字段的最小版（run 级的 versions 由 runner 补）。"""
    index = int(payload["index"])
    now = "2026-01-01T00:00:00+00:00"
    return {
        "case_id": case_id,
        "case_type": case_type,
        "case_dir": str(payload["case_dir"]),
        "version": "",
        "content_summary": f"{case_id}·{case_type}",
        "session_id": f"fake-{index}",
        "passed": passed,
        "reason": reason,
        "multi_turn": False,
        "started_at": now,
        "ended_at": now,
        "error": "",
        "agents": {"under_test": True, "simulator": False, "judge": False},
        "models": {"under_test": "fake-model", "simulator": "none", "judge": "none"},
    }


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
