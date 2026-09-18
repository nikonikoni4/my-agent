"""lifeprism 的环境机制（`evaluate.core` 的机制层实现）。

一个「环境」= 一份按声明从只读底座拼出来的独立数据根，一条用例独占一份。

三条纪律（对应 `evaluate/core/types.py` 的 `EnvProvider` 契约）：

1. **只从 `EnvSpec.template` 取初始状态**：环境里的一切都来自底座，不顺带读进程里的
   其他全局路径，否则模板就不再是唯一真源。
2. **建与重置是同一条路径**：`reset_env` 就是「删掉 + 再建一遍」，与 `create_env`
   共用 `_build`。两边只要有一点不同，重置就会漏掉那一项，而漏了不报错。
   不维护「哪些文件会变」的清单——那张清单永远会漏（漏一项就让上一条用例的写入串进
   下一条，且表现为「结果不对但没报错」）。
3. **环境数据根对宿主可见**：就是本地目录，证据采集与结果回收直接本地读写。

**环境是按配置拼的，不是「底座整份复制」**：只复制这次要用的那点东西（配置见
`cases.yaml` 的 `meta.env`）——提示词必复制，底座里与本次无关的历史数据（实测 393 文件 /
122MB，其中 88% 是日记文件数、绝大部分体积是一个 120.7MB 的库）不必每条用例都搬一遍。

**边界：什么进 core 的 `EnvSpec`，什么留在这里。** `evaluate/core/types.py` 的
`EnvSpec` 只说一件事——"从哪个只读模板取初始状态"（`template` / `label`）；它用的词汇
必须与机制无关，换任何 provider 都成立。而"复制哪些路径、`lifewatch_ai.db` 要不要建成
空壳、留哪几张表"是 **lifeprism 的领域知识**：换个 provider（纯文件状态、别的数据库），
没人能解释这些字段，只能静默忽略——正是那边「解耦纪律」要消灭的那类失败。所以它们以
`EnvConfig` 的形式当**本 provider 的构造参数**传进来，不进 core 的契约。

执行通道（`create_worker`）产出的是**子进程 worker**（见 [worker.py](./worker.py)）：
被测系统把数据根放在模块级全局里（`lifeprismevalue.config` 的 `use_data_path`），
同一进程内两份数据根无法共存——槽位要真正并行，一条任务就必须独占一个进程。
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Iterable

from evaluate.core import Env, EnvProvider, EnvSpec, Worker

from lifeprismevalue.evalue.sqlite_read import open_readonly, quote_identifier, table_names
from lifeprismevalue.evalue.types import DB_MODE_COPY, DB_MODE_EMPTY, DbConfig, EnvConfig

logger = logging.getLogger(__name__)

# 环境目录名的前缀：`envs/<key>/`，key 形如 `env01`（同一个槽位复用时 key 不变）
ENV_KEY_PREFIX = "env"

# 提示词版本库在数据根里的位置：被测 agent 从它取提示词（见 lifeprismevalue.prompts），
# run.json 的「提示词版本」也从它读。它必须进环境，否则跑的不是录下来的那一版。
PROMPT_BOOK_REL = "prompts/agent_prompts.yaml"


class LifeprismEnv(Env):
    """一份独立的数据根（`envs/<key>/`）。

    `Env.root` / `Env.key` 是抽象 **property**，只能以 property 实现：写成 dataclass
    字段不会覆盖抽象 property，类会一直是抽象的（实例化直接报
    "Can't instantiate abstract class"）。

    句柄只记「材料从哪个底座来」（`template`）：core 的 `reset_env(env)` 只传环境，
    重置就靠它回到同一个底座；至于"复制哪些路径、库怎么造"，在 provider 的 `env_config`
    里（那是机制自己的配置，与这份句柄无关）。
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
        """环境里的材料从哪来（只读底座）。属于 provider 的内部事实，core 不看。"""
        return self._template


class LifeprismEnvProvider(EnvProvider):
    """文件夹环境机制：造 = 按声明从底座拼一份；重置 = 删掉再拼一遍；销毁 = 删掉。

    Args:
        envs_root: 环境目录的父目录（本次 run 的 `envs/`）。
        ipc_dir: 子进程请求 / 结果文件的落点（本次 run 的 `ipc/`）。
        log_dir: 子进程 stdout/stderr 的落点（本次 run 的 `logs/`）；None 表示丢弃。
        python: 跑子进程的解释器，默认与父进程同一个。
        env_config: **本次 run 的环境配置**（`cases.yaml` 的 `meta.env`）：复制哪些路径、
            库怎么造。这是机制自己的材料清单，按 `evaluate/core/types.py` 的纪律走构造参数，
            不进 core 的 `EnvSpec`——core 只认"从 `template` 取初始状态"这一件事。
    """

    def __init__(
        self,
        *,
        envs_root: str | Path,
        ipc_dir: str | Path,
        env_config: EnvConfig,
        log_dir: str | Path | None = None,
        python: str | None = None,
    ) -> None:
        self.envs_root = Path(envs_root)
        self.ipc_dir = Path(ipc_dir)
        self.env_config = env_config
        self.log_dir = Path(log_dir) if log_dir is not None else None
        self.python = python or sys.executable
        self._counter = 0
        self._lock = threading.Lock()

    # ---------- 环境生命周期 ----------

    def create_env(self, spec: EnvSpec) -> LifeprismEnv:
        """造一个全新环境：按本 provider 的环境配置，从 `spec.template` 拼一份。

        每个槽位只做一次，之后靠 `reset_env` 复用。
        """
        with self._lock:
            self._counter += 1
            key = f"{ENV_KEY_PREFIX}{self._counter:02d}"

        root = self.envs_root / key
        self._build(root, spec.template)
        logger.info("造环境：%s（底座 %s）", root, spec.template)
        return LifeprismEnv(root=root, key=key, template=spec.template)

    def reset_env(self, env: LifeprismEnv) -> None:
        """整份回滚：删掉整棵树，照同一份配置重新拼一遍。

        判据是「用过没有」而不是「上次失没失败」——成功同样会脏（用例会写数据），
        按失败判断会让下一条用例继承上一条的写入。

        配置来自 provider（造环境时用的就是它），底座来自句柄：重置与创建走**同一条
        路径、同一份材料**，否则两边迟早分叉，而分叉不报错。
        """
        assert isinstance(env, LifeprismEnv), f"不认识的环境类型：{type(env).__name__}"
        self._build(env.root, env.template)
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

    # ---------- 建环境 ----------

    def _build(self, root: Path, template: Path) -> None:
        """按本 provider 的环境配置拼出一份环境（create 与 reset 共用的唯一实现）。

        末态是确定的：**只有配置里列的东西存在**。所以先整棵删掉——残留（上次的同名
        目录、上一条用例写下的文件）不会被「碰巧留在原地」。
        """
        config = self.env_config
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)

        for rel in (*config.copy, *config.extra):
            _copy_from_template(template, root, rel)

        if config.db is not None:
            _build_db(config.db, template=template, root=root)


# ---------------- 建环境的材料 ----------------


def _copy_from_template(template: Path, root: Path, rel: str) -> None:
    """把底座里的 `rel`（文件或目录）复制进环境。

    底座里没有这个路径就报错，不静默跳过：静默跳过会让环境「少一块」却照常跑完，
    用例挂掉的原因看着像被测对象的问题。
    """
    src, dst = template / rel, root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        # 声明的路径可能互相嵌套（如 agent/ 与 agent/chat/custom_prompt.md）
        shutil.copytree(src, dst, dirs_exist_ok=True)
    elif src.is_file():
        shutil.copy2(src, dst)
    else:
        raise FileNotFoundError(f"底座里没有声明的路径: {rel}（底座 {template}）")


def _build_db(db: DbConfig, *, template: Path, root: Path) -> None:
    """按库配方造库：整库复制，或只建结构 + 回填 `keep_tables`。"""
    src_db = template / db.path
    if not src_db.is_file():
        raise FileNotFoundError(f"底座里没有声明的数据库: {db.path}（底座 {template}）")
    dst_db = root / db.path
    dst_db.parent.mkdir(parents=True, exist_ok=True)

    if db.mode == DB_MODE_COPY:
        shutil.copy2(src_db, dst_db)
        return

    src = open_readonly(src_db)
    con = sqlite3.connect(dst_db)
    try:
        _create_schema(src, con)
        _fill_tables(src, con, db.keep_tables)
    finally:
        con.close()
        src.close()


def _create_schema(src: sqlite3.Connection, con: sqlite3.Connection) -> None:
    """把源库的结构整体搬进新库：表先建，索引 / 视图 / 触发器随后。

    行一行不搬（`sqlite_%` 内部表也跳过，`sqlite_sequence` 会在回填带 AUTOINCREMENT
    的表时由 SQLite 自己维护）。

    必须显式 `BEGIN`：Python 的 sqlite3 **不把 DDL 算进隐式事务**，不写这一句就是
    140 条建表 / 建索引各自提交各自 fsync（实测 0.97s → 0.02s）。
    """
    objects = [
        (kind, sql)
        for kind, sql in src.execute(
            "select type, sql from sqlite_master where sql is not null and name not like 'sqlite_%'"
        )
    ]
    ordered = [sql for kind, sql in objects if kind == "table"]
    ordered += [sql for kind, sql in objects if kind != "table"]

    con.execute("BEGIN")
    for statement in ordered:
        con.execute(statement)
    con.execute("COMMIT")


def _fill_tables(
    src: sqlite3.Connection, con: sqlite3.Connection, keep_tables: Iterable[str]
) -> None:
    """把 `keep_tables` 的行整表搬进新库；表在底座里不存在就报错。

    「空库」不等于「全空」：枚举 / 注册 / 字典表与判分要看的历史基线都属于**初始状态**
    的一部分。它们空了，用例失败的原因就与被测对象无关了（例如打卡类用例要先查到
    习惯再打卡，习惯表空了必然失败）。
    """
    tables = list(keep_tables)
    if not tables:
        return

    con.execute("BEGIN")
    for table in tables:
        name = quote_identifier(table)
        columns = [row[1] for row in src.execute(f"PRAGMA table_info({name})")]
        if not columns:
            raise ValueError(f"keep_tables 里的表在底座里不存在: {table}")
        rows = list(src.execute(f"select * from {name}"))
        placeholders = ",".join("?" * len(columns))
        con.executemany(f"insert into {name} values ({placeholders})", rows)
    con.execute("COMMIT")


# ---------------- 开槽前的自检 ----------------


def check_env_inputs(template: Path, config: EnvConfig) -> None:
    """检查环境配置里的路径 / 库 / 表在底座里都真的在；不在就报错。

    放在开槽之前（而不是等 provider 建环境时）是为了**跑之前就报错**：建环境发生在
    槽位线程里，那时报错只留下「某条任务失败」；在这里报错则整个 run 不启动，
    原因也直白。名字打错、底座换代导致表没了，都会在这一步拦住。

    另外守住两条必需项：**提示词必须在环境里**（它是环境跑起来的前提，也是
    「这次跑的是哪版提示词」的依据）；**复制清单不能空**（空清单会造出一个没有提示词的
    环境，用例全挂却看着像被测对象的问题）。
    """
    if not config.copy:
        raise ValueError("meta.env.copy 不能为空：环境要有初始状态的必需来源（如提示词）")

    for rel in (*config.copy, *config.extra):
        if not (template / rel).exists():
            raise FileNotFoundError(f"底座里没有声明的路径: {rel}（底座 {template}）")

    if not _covers(config.copy, PROMPT_BOOK_REL):
        raise ValueError(
            f"env.copy 必须覆盖 {PROMPT_BOOK_REL}（提示词是环境的必需项）："
            f"现在只有 {list(config.copy)}"
        )

    if config.db is None:
        return
    db_path = template / config.db.path
    if not db_path.is_file():
        raise FileNotFoundError(f"底座里没有声明的数据库: {config.db.path}（底座 {template}）")

    if config.db.mode != DB_MODE_EMPTY or not config.db.keep_tables:
        # 整库复制、或空库但一张表都不回填：都不必读底座库的结构
        return
    known = table_names(db_path)
    missing = [table for table in config.db.keep_tables if table not in known]
    if missing:
        raise ValueError(
            f"keep_tables 里的表在底座里不存在: {missing}（底座库 {config.db.path}）"
        )


def _covers(paths: Iterable[str], rel: str) -> bool:
    """声明里有没有覆盖 rel：等于它，或声明了它的某个父目录。"""
    target = Path(rel)
    return any(target == Path(raw) or Path(raw) in target.parents for raw in paths)
