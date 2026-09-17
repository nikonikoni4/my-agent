"""lifeprism 的环境机制（`evaluate.core` 的机制层实现）。

一个「环境」= 一份从只读底座复制出来的独立数据根，一条用例独占一份。

三条纪律（对应 `evaluate/core/types.py` 的 `EnvProvider` 契约）：

1. **只从 `EnvSpec.template` 取初始状态**：环境里的一切都来自底座，不顺带读进程里的
   其他全局路径，否则模板就不再是唯一真源。
2. **`reset_env` 是整份回滚**：删掉环境目录、照模板重复制。不维护「哪些文件会变」的
   清单——那张清单永远会漏，漏了还不报错（漏一项就让上一条用例的写入串进下一条，且
   表现为「结果不对但没报错」）。
3. **环境数据根对宿主可见**：就是本地目录，证据采集与结果回收直接本地读写。

执行通道（`create_worker`）产出的是**子进程 worker**（见 [worker.py](./worker.py)）：
被测系统把数据根放在模块级全局里（`lifeprismevalue.config` 的 `use_data_path`），
同一进程内两份数据根无法共存——槽位要真正并行，一条任务就必须独占一个进程。
"""

from __future__ import annotations

import logging
import shutil
import sys
import threading
from pathlib import Path

from evaluate.core import Env, EnvProvider, EnvSpec, Worker

logger = logging.getLogger(__name__)

# 环境目录名的前缀：`envs/<key>/`，key 形如 `env01`（同一个槽位复用时 key 不变）
ENV_KEY_PREFIX = "env"


class LifeprismEnv(Env):
    """一份独立的数据根（`envs/<key>/`）。

    `Env.root` / `Env.key` 是抽象 **property**，只能以 property 实现：写成 dataclass
    字段不会覆盖抽象 property，类会一直是抽象的（实例化直接报
    "Can't instantiate abstract class"）。
    """

    def __init__(self, *, root: Path, key: str, template: Path) -> None:
        self._root = Path(root)
        self._key = key
        self._template = Path(template)

    @property
    def root(self) -> Path:
        """环境的数据根（宿主进程可直接读写）。"""
        return self._root

    @property
    def key(self) -> str:
        """环境的稳定标识：日志、目录命名、失败排查时定位现场用。"""
        return self._key

    @property
    def template(self) -> Path:
        """这个环境回滚到哪个基线。属于 provider 的内部事实，core 不看。"""
        return self._template


class LifeprismEnvProvider(EnvProvider):
    """文件夹环境机制：造 = 复制底座；重置 = 删掉重复制；销毁 = 删掉。

    Args:
        envs_root: 环境目录的父目录（本次 run 的 `envs/`）。
        ipc_dir: 子进程请求 / 结果文件的落点（本次 run 的 `ipc/`）。
        log_dir: 子进程 stdout/stderr 的落点（本次 run 的 `logs/`）；None 表示丢弃。
        python: 跑子进程的解释器，默认与父进程同一个。
    """

    def __init__(
        self,
        *,
        envs_root: str | Path,
        ipc_dir: str | Path,
        log_dir: str | Path | None = None,
        python: str | None = None,
    ) -> None:
        self.envs_root = Path(envs_root)
        self.ipc_dir = Path(ipc_dir)
        self.log_dir = Path(log_dir) if log_dir is not None else None
        self.python = python or sys.executable
        self._counter = 0
        self._lock = threading.Lock()

    # ---------- 环境生命周期 ----------

    def create_env(self, spec: EnvSpec) -> LifeprismEnv:
        """按模板造一个全新环境：整棵复制底座。

        贵操作（底座实测 122MB / 393 文件），每个槽位只做一次，之后靠 `reset_env` 复用。
        """
        with self._lock:
            self._counter += 1
            key = f"{ENV_KEY_PREFIX}{self._counter:02d}"

        root = self.envs_root / key
        if root.exists():
            # 同名残留（同一个 run 目录被重复使用）先清掉，否则 copytree 会直接报错
            shutil.rmtree(root)
        shutil.copytree(spec.template, root)
        logger.info("造环境：%s（模板 %s）", root, spec.template)
        return LifeprismEnv(root=root, key=key, template=spec.template)

    def reset_env(self, env: LifeprismEnv) -> None:
        """整份回滚：删掉整棵树，照模板重复制一遍。

        判据是「用过没有」而不是「上次失没失败」——成功同样会脏（用例会写数据），
        按失败判断会让下一条用例继承上一条的写入。
        """
        assert isinstance(env, LifeprismEnv), f"不认识的环境类型：{type(env).__name__}"
        shutil.rmtree(env.root)
        shutil.copytree(env.template, env.root)
        logger.debug("回滚环境：%s", env.root)

    def dispose_env(self, env: LifeprismEnv) -> None:
        """销毁环境（与 reset 是两件事：这是「不要了」）。"""
        assert isinstance(env, LifeprismEnv), f"不认识的环境类型：{type(env).__name__}"
        shutil.rmtree(env.root, ignore_errors=True)

    # ---------- 执行通道 ----------

    def create_worker(self, env: LifeprismEnv) -> Worker:
        """为环境造一个子进程执行通道（一对一）。

        延迟导入：worker 侧需要 `Env` 句柄（子进程里重建），顶层互相 import 会成环。
        """
        from lifeprismevalue.evalue.worker import SubprocessWorker

        return SubprocessWorker(
            env=env,
            ipc_dir=self.ipc_dir,
            log_dir=self.log_dir,
            python=self.python,
        )
