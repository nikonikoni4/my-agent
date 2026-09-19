"""测试用的假用例执行实体：**子进程里真正跑起来的那一个**。

放在包内（而不是测试文件里）是因为子进程要用 `"module.path:function"` 把它 import
回来——测试文件的模块名不在这个约定里。它只跟评测内核打交道（读 payload、写环境、
返回 `CaseResult` 形状的字段），不碰 LLM，所以整条「runner → EvalCore → 子进程 →
领域层 → 落盘 → 重建报表」的链路可以离线跑。

行为由**用例 id 的前缀**决定（cases.yaml 由测试自己写，故这是可控的约定）：

| id 前缀 | 行为 | 用来验什么 |
| --- | --- | --- |
| `fail` | 抛异常 | 执行通道失败 → 记 error、保留现场 |
| `crash` | `os._exit(3)`（不写任何产物） | 子进程非零退出 / 没产出结果 |
| `observe` | 先读上一条用例留下的标记，再写自己的 | 槽位复用前必须整份回滚（读到的必须是空） |
| `sleep` | 睡 0.3s，把起止时间写进 `case_dir/events.json` | 槽位真的并行（时间区间重叠） |
| 其它 | 正常返回 | 同序、产物、summary |

**产物必须按真契约落齐**。重建报表是从产物算出来的，少落一份，run 级测试就会在
「报表某列为什么是空的」上给出假结论。所以：

- 产物的**形状**一律用真实现（`snapshot_case` / 回执三字段 / `collect_agent_info` /
  `content_summary`），本文件不另外定义一遍；
- 用到的角色各落一份 `session_<角色>.jsonl`（真跑时是 g 步复制改名），判了就落
  `judge.json`——不落它，报表里的「是否通过 / 得分」永远是空的。

正常返回的那条带 `score`（按无评分要求时的二值口径：通过 = 100，不通过 = 0），
好让 run 级链路也能验到报表的「得分」列。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from lifeprismevalue.evalue.case import (
    JUDGE,
    SIMULATOR,
    UNDER_TEST,
    collect_agent_info,
    content_summary,
    dumped_session_path,
    snapshot_case,
)
from lifeprismevalue.evalue.caseload import load_case_set
from lifeprismevalue.evalue.types import (
    JUDGE_FILENAME,
    RESULT_FILENAME,
    Case,
)

# 环境里用来留下「被写过」痕迹的文件名（宿主可直接读，故也是 environment 可见性的证据）
MARKER = "marker.txt"

# sleep 行为的固定时长（秒）：要比「并发子进程之间的启动抖动」明显长一截，
# 否则「两个槽位的区间是否重叠」会被启动快慢影响（见 test_runner 的并行用例）。
SLEEP_SECONDS = 0.8

# 结果里写死的时间与模型名：让「重建出来的行」有稳定值可断言
STAMP = "2026-01-01T00:00:00+00:00"
MODEL = "fake-model"


def execute(payload: dict, env: Any) -> dict:
    """假用例的入口（`fn(payload, env) -> dict`）。"""
    case = _load_case(payload)
    case_dir = Path(payload["case_dir"])
    case_dir.mkdir(parents=True, exist_ok=True)

    if case.id.startswith("fail"):
        raise RuntimeError("故意失败")
    if case.id.startswith("crash"):
        os._exit(3)  # 模拟子进程被杀 / 崩掉：不写任何产物

    if case.id.startswith("observe"):
        marker = env.root / MARKER
        observed = marker.read_text(encoding="utf-8") if marker.exists() else ""
        marker.write_text(f"被用例 {payload['index']} 写过", encoding="utf-8")
        _write(case_dir / "observed.json", {"observed": observed})
        return _finish(
            case_dir, case, payload, passed=True, reason=f"observed={observed!r}"
        )

    if case.id.startswith("sleep"):
        started = time.time()
        time.sleep(SLEEP_SECONDS)
        _write(case_dir / "events.json", {"start": started, "end": time.time()})
        return _finish(case_dir, case, payload, passed=True, reason="sleep")

    # 默认行为：写环境、落产物、返回一份「通过」的结果
    (env.root / MARKER).write_text("写过", encoding="utf-8")
    passed = bool(payload.get("passed", True))
    # 得分跟着判定走（无评分要求时的二值口径：通过 = 满分，不通过 = 0）
    return _finish(
        case_dir, case, payload, passed=passed, score=100 if passed else 0
    )


def _finish(
    case_dir: Path,
    case: Case,
    payload: dict,
    *,
    passed: bool | None,
    score: int | None = None,
    reason: str = "",
    error: str = "",
) -> dict:
    """把一次「跑过了」的产物落齐，再回传结果（形状与 `case.py:_result` 一致）。"""
    snapshot_case(case, case_dir)
    _write(case_dir / RESULT_FILENAME, {"started_at": STAMP, "ended_at": STAMP, "error": error})

    index = int(payload["index"])
    session_id = f"fake-{index}"
    roles = [UNDER_TEST]
    if case.uses_judge:
        roles.append(JUDGE)
    if case.uses_simulator:
        roles.append(SIMULATOR)
    for role in roles:
        # 真跑时各角色的 session_id 各不相同，这里也分开，免得测试里张冠李戴看不出来
        _write_session(
            case_dir, role, session_id if role == UNDER_TEST else f"{session_id}-{role}"
        )

    if case.uses_judge:
        _write(
            case_dir / JUDGE_FILENAME,
            {"pass": passed, "score": score, "reason": reason, "parse_error": ""},
        )

    # agents / models 由**真函数**算出来——假实体不自己编，否则它和真链路会在
    # 「报表那一列该是什么」上各说各话，而 run 级测试正是拿它当准绳的
    agents, models = collect_agent_info(case, case_dir)
    return {
        "case_id": case.id,
        "case_type": case.type,
        "case_dir": str(case_dir),
        "version": "",
        "content_summary": content_summary(case),
        "session_id": session_id,
        "passed": passed,
        "score": score,
        "reason": reason,
        "multi_turn": case.multi_turn,
        "started_at": STAMP,
        "ended_at": STAMP,
        "error": error,
        "agents": agents,
        "models": models,
    }


def _load_case(payload: dict) -> Case:
    """按 payload 里的 index 取出这条用例。

    走**真的** `load_case_set`（而不是自己读 yaml 拼一个），这样假实体对用例的理解
    与真链路完全一致——包括 `input_mode` / `judge.mode` 这些决定"该落哪几份产物"的开关。
    """
    return load_case_set(payload["cases_path"]).cases[int(payload["index"])]


def _write_session(case_dir: Path, role: str, session_id: str) -> None:
    """落一份最小的 session 副本（`meta_data` / `request/header` / `turn/end` 三条）。

    重建报表从这三条里分别取 session_id、模型名、运行终态，所以缺哪条对应列就空。
    """
    lines = [
        {"type": "meta_data", "session_id": session_id},
        {"type": "request/header", "data": {"model_name": MODEL}},
        {"type": "turn/end", "turn": 1, "data": {"reason_type": "success", "reason_text": "", "error_type": ""}},
    ]
    path = dumped_session_path(case_dir, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8"
    )


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
