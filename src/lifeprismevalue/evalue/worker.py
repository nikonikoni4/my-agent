"""执行通道：一条任务 = 一条子进程。

**为什么必须是子进程**（而不是线程）：被测系统把「数据根」放在模块级全局里
（`lifeprismevalue.config.use_data_path`），同一个模块在一个解释器里只能有一份实现，
两份数据根无法在同一进程共存。要让槽位真正并行，每条任务必须独占一个进程——这也是
`evaluate/core/types.py` 的「隔离粒度」一节写下的约束。

## 进程间协议

父子两侧各持一份，谁也不 import 谁的领域代码（本模块只认 `WorkerTask` 的字符串契约）：

    子进程启动：python -m lifeprismevalue.evalue.worker <请求.json> <结果.json>

    请求.json = {
        "entrypoint": "module.path:function",         # WorkerTask 的入口契约
        "payload": {...},                             # 任务载荷，对 core 不透明
        "env": {"root": "...", "key": "...", "template": "..."},   # 子进程据此重建 Env 句柄
    }
    结果.json = {"ok": true,  "output": {...}}
              | {"ok": false, "error": "类型: 消息"}

子进程的 stdout/stderr 重定向到 `<log_dir>/<用例目录名>.log`（用例跑成什么样、异常链
是什么，都在里面；失败时从尾部摘一段进 `WorkerOutcome.error`）。

## 失败怎么表现

- 子进程正常退出但结果 `ok=false`（executor 自己崩了）→ 抛异常，core 收进
  `WorkerOutcome.error`；该槽位按 `keep_env_on_failure` 决定是否保留现场。
- 子进程非零退出 / 没产出结果（导入失败、被杀）→ 同样抛异常，错误信息里带上 exit code
  与日志尾部。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import traceback
import uuid
from pathlib import Path
from types import TracebackType
from typing import Any, IO

from evaluate.core import Worker, WorkerTask

from lifeprismevalue.evalue.env import LifeprismEnv

logger = logging.getLogger(__name__)

# 子进程入口模块（`python -m <本模块>`）
WORKER_MODULE = "lifeprismevalue.evalue.worker"

# 错误信息里摘取的日志尾部长度 / 落盘日志的保留上限（字符）
ERROR_TAIL_CHARS = 2000


class SubprocessWorker(Worker):
    """把一份任务丢给一条子进程去跑，同步等它结束。

    `env` 由 `create_worker(env)` 注入并存成属性——它知道自己的工作目录；`run(task)`
    因此不含 env：不变的东西走构造参数，变的东西走调用参数。
    """

    def __init__(
        self,
        *,
        env: LifeprismEnv,
        ipc_dir: str | Path,
        log_dir: str | Path | None = None,
        python: str | None = None,
    ) -> None:
        self._env = env
        self._ipc_dir = Path(ipc_dir)
        self._log_dir = Path(log_dir) if log_dir is not None else None
        self._python = python or sys.executable

    def run(self, task: WorkerTask) -> dict[str, Any]:
        """起一条子进程跑 `task`，返回 entrypoint 的产出。

        抛异常即视为该份任务失败：core 捕获后收进 `WorkerOutcome.error`。
        """
        self._ipc_dir.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        request_path = self._ipc_dir / f"{token}.request.json"
        result_path = self._ipc_dir / f"{token}.result.json"
        log_path = self._log_path(task, token)

        request_path.write_text(
            json.dumps(
                {
                    "entrypoint": task.entrypoint,
                    "payload": task.payload,
                    "env": {
                        "root": str(self._env.root),
                        "key": self._env.key,
                        "template": str(self._env.template),
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        logger.info(
            "起子进程跑任务：env=%s entrypoint=%s 日志=%s",
            self._env.key,
            task.entrypoint,
            log_path or "（丢弃）",
        )
        with _LogStream(log_path) as stream:
            completed = subprocess.run(
                [self._python, "-m", WORKER_MODULE, str(request_path), str(result_path)],
                env=self._child_env(),
                stdout=stream,
                stderr=subprocess.STDOUT,
            )

        data = _read_json(result_path)
        if isinstance(data, dict) and data.get("ok") is True:
            request_path.unlink(missing_ok=True)
            result_path.unlink(missing_ok=True)
            output = data.get("output")
            return dict(output) if isinstance(output, dict) else {}

        # 失败：ipc 文件留着，配合日志一起排查
        detail = str((data or {}).get("error") or "").strip() or _tail(log_path)
        raise RuntimeError(
            f"子进程未正常完成（exit={completed.returncode}）"
            + (f"：{detail}" if detail else "")
        )

    # ---------- 内部 ----------

    def _log_path(self, task: WorkerTask, token: str) -> Path | None:
        """子进程日志路径：用用例目录名命名，便于人对上号。"""
        if self._log_dir is None:
            return None
        case_dir = task.payload.get("case_dir")
        name = Path(str(case_dir)).name if case_dir else token
        return self._log_dir / f"{name}.log"

    def _child_env(self) -> dict[str, str]:
        """子进程环境变量：继承本进程 + 把父进程的 sys.path 透传为 PYTHONPATH。

        父进程能 import 到的模块（`evaluate` / `lifeprismevalue` / `myagent`），子进程
        必须也能，否则 entrypoint 解析不了。依赖（API key、模型名）也走环境变量继承。
        """
        env = dict(os.environ)
        parts = [p for p in sys.path if p]
        existing = env.get("PYTHONPATH", "")
        if existing:
            parts.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(parts))
        # 日志会被重定向到文件，固定成 UTF-8：避免子进程按 Windows 本地编码写、
        # 父进程按 UTF-8 读时出现乱码
        env["PYTHONIOENCODING"] = "utf-8"
        return env


class _LogStream:
    """子进程日志流：有 log_dir 就落文件，否则丢弃。"""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._stream: IO[bytes] | None = None

    def __enter__(self) -> IO[bytes]:
        if self._path is None:
            return subprocess.DEVNULL
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self._path.open("wb")
        return self._stream

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._stream is not None:
            self._stream.close()


def _read_json(path: Path) -> Any:
    """读 JSON；文件不存在或不是合法 JSON 时返回 None（交给调用方兜底）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _tail(path: Path | None, limit: int = ERROR_TAIL_CHARS) -> str:
    """读日志尾部的文字，压成一行（错误信息里只放得下这个量级）。"""
    if path is None or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail = text[-limit:]
    return " ".join(tail.split())


# ---------------- 子进程入口 ----------------


def main(argv: list[str] | None = None) -> int:
    """子进程入口：`python -m lifeprismevalue.evalue.worker <请求> <结果>`。

    只做三件事：重建 `Env` 句柄 → 按 `entrypoint` 找函数 → 把产出写成结果文件。
    领域流程（切数据根、跑用例）都在 entrypoint 里，本模块不认识它。

    Returns:
        进程退出码：0 = 跑完（无论用例本身通过与否）；1 = 执行通道失败；2 = 用法错误。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("用法：python -m lifeprismevalue.evalue.worker <请求.json> <结果.json>", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    request_path, result_path = Path(args[0]), Path(args[1])
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        raw_env = request.get("env") or {}
        env = LifeprismEnv(
            root=Path(raw_env["root"]),
            key=str(raw_env.get("key") or ""),
            template=Path(raw_env["template"]),
        )
        entrypoint_name = str(request["entrypoint"])
        logger.info("子进程 %s 启动：env=%s entrypoint=%s", os.getpid(), env.key, entrypoint_name)
        entrypoint = WorkerTask.load_entrypoint(entrypoint_name)
        output = entrypoint(request.get("payload") or {}, env)
        logger.info("子进程 %s 完成", os.getpid())
    except BaseException as e:  # noqa: BLE001 - 任何异常都要变成结果文件 + 退出码
        traceback.print_exc()
        _write_result(result_path, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        return 1

    _write_result(result_path, {"ok": True, "output": output})
    return 0


def _write_result(path: Path, data: dict[str, Any]) -> None:
    """写结果文件（父进程读不到它会退化成看退出码与日志）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
