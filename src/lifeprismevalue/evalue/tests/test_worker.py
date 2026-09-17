"""执行通道：跨进程协议（真起子进程，跑假 entrypoint）。

验的是「一条任务 = 一条子进程」这条通道本身：产出怎么回传、环境数据根是不是子进程
自己的、异常与崩溃怎么表现、日志落在哪。用例语义不在这里（见 test_case.py）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from evaluate.core import WorkerTask

from lifeprismevalue.evalue.env import LifeprismEnv
from lifeprismevalue.evalue.worker import SubprocessWorker
from helpers import fake_cases, write_cases

FAKE_ENTRYPOINT = "lifeprismevalue.evalue.tests.fake_case:execute"


def make_worker(tmp_path: Path) -> SubprocessWorker:
    env_root = tmp_path / "env01"
    env_root.mkdir(parents=True, exist_ok=True)
    env = LifeprismEnv(root=env_root, key="env01", template=tmp_path / "base")
    return SubprocessWorker(env=env, ipc_dir=tmp_path / "ipc", log_dir=tmp_path / "logs")


def make_task(
    tmp_path: Path, body: str, *, index: int = 0, entrypoint: str = FAKE_ENTRYPOINT
) -> WorkerTask:
    return WorkerTask(
        entrypoint=entrypoint,
        payload={
            "cases_path": str(write_cases(tmp_path, body)),
            "index": index,
            "case_dir": str(tmp_path / f"000_case{index}"),
            "baseline_dir": str(tmp_path / f"000_case{index}" / "baseline"),
            "session_folder": str(tmp_path / "sessions"),
            "turn_timeout": 5.0,
            "scan_other_changed_files": True,
        },
    )


def test_跑通_产出与产物都对(tmp_path) -> None:
    worker = make_worker(tmp_path)
    task = make_task(tmp_path, fake_cases("ok-1"))

    output = worker.run(task)

    assert output["case_id"] == "ok-1"
    assert output["passed"] is True
    # 子进程拿到的数据根就是该槽位的环境（假 entrypoint 往它写了一个标记）
    assert (worker._env.root / "marker.txt").read_text(encoding="utf-8") == "写过"
    assert (Path(task.payload["case_dir"]) / "case.yaml").exists()


def test_日志按用例目录名落盘(tmp_path) -> None:
    worker = make_worker(tmp_path)
    task = make_task(tmp_path, fake_cases("ok-1"))

    worker.run(task)

    log = tmp_path / "logs" / "000_case0.log"
    assert log.is_file()
    # 子进程自己打的启动 / 完成行（排查时先看这个文件）
    text = log.read_text(encoding="utf-8")
    assert "启动" in text and "完成" in text


def test_成功时清掉通信文件(tmp_path) -> None:
    worker = make_worker(tmp_path)
    worker.run(make_task(tmp_path, fake_cases("ok-1")))

    assert list((tmp_path / "ipc").iterdir()) == []


def test_entrypoint_抛异常_worker_抛错并带上原因(tmp_path) -> None:
    worker = make_worker(tmp_path)
    task = make_task(tmp_path, fake_cases("fail-1"))

    with pytest.raises(RuntimeError, match="故意失败"):
        worker.run(task)

    # 失败时通信文件留着，配合日志一起排查
    assert list((tmp_path / "ipc").iterdir())


def test_子进程非零退出且没产出结果(tmp_path) -> None:
    worker = make_worker(tmp_path)
    task = make_task(tmp_path, fake_cases("crash-1"))

    with pytest.raises(RuntimeError, match="exit=3"):
        worker.run(task)


def test_entrypoint_解析失败_给出可读错误(tmp_path) -> None:
    worker = make_worker(tmp_path)
    task = make_task(tmp_path, fake_cases("ok-1"), entrypoint="根本没这个模块:execute")

    with pytest.raises(RuntimeError, match="ModuleNotFoundError"):
        worker.run(task)


def test_请求里带的是_module_function_与不透明载荷(tmp_path) -> None:
    """协议本身：请求 = `module:function` 字符串 + 载荷 + 环境三要素。

    用「entrypoint 解析不了」的失败路径把请求文件留在磁盘上读它（成功会清掉通信文件）。
    """
    worker = make_worker(tmp_path)
    task = make_task(tmp_path, fake_cases("ok-1"), entrypoint="根本没这个模块:execute")

    with pytest.raises(RuntimeError):
        worker.run(task)

    (request_file,) = (tmp_path / "ipc").glob("*.request.json")
    request = json.loads(request_file.read_text(encoding="utf-8"))

    assert request["entrypoint"] == "根本没这个模块:execute"
    assert request["payload"]["index"] == 0
    assert request["env"] == {
        "root": str(worker._env.root),
        "key": "env01",
        "template": str(tmp_path / "base"),
    }
