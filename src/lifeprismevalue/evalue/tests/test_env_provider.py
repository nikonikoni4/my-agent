"""环境机制的语义：按配置造 / 整份回滚 / 销毁 / 出执行通道 / 开槽前自检。

这几条是槽位池能成立的前提（见 evaluate/core/types.py 的 EnvProvider 契约），
尤其「整份回滚」——它替代了上一版「按可变路径清单还原」的做法，判据从「猜哪些会变」
变成「整体回配置态」。

另一半是这一版的要点：**环境是按配置拼的，不是底座整份复制**（配置 = cases.yaml 的
`meta.env`，作为 provider 的构造参数传进来）。所以这里既验「配置里列的东西都来了」，
也验「没列的没来」（不然 122MB 底座又会悄悄整份搬一遍）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from evaluate.core import Env, EnvProvider, EnvSpec

from lifeprismevalue.evalue.env import (
    PROMPT_BOOK_REL,
    LifeprismEnvProvider,
    check_env_inputs,
)
from lifeprismevalue.evalue.types import DB_MODE_COPY, DB_MODE_EMPTY, DbConfig, EnvConfig
from lifeprismevalue.evalue.worker import SubprocessWorker

DB_REL = "dataset/lifewatch_ai.db"
CP_REL = "agent/chat/custom_prompt.md"
DIARY_REL = "diary/2025/01/01.md"
PROMPT_REL = "prompts/agent_prompts.yaml"
USER_REL = "user/user.md"

# 建空库时要整表回填的表（枚举 / 注册 / 判分看的历史基线）
KEEP_TABLES = ("custom_record_types", "mood_types", "custom_expense_log")


# ---------------- 底座 / 配置 ----------------


def make_template_db(path: Path) -> None:
    """造一个真 sqlite：要保留的小表 + 一个索引 + 大批噪声行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    with con:
        con.execute("CREATE TABLE custom_record_types (id TEXT, slug TEXT)")
        con.execute("INSERT INTO custom_record_types VALUES ('t1','支出记录'),('t2','心情记录')")
        con.execute("CREATE TABLE mood_types (id TEXT, name TEXT)")
        con.execute("INSERT INTO mood_types VALUES ('m1','喜悦')")
        con.execute("CREATE TABLE custom_expense_log (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT)")
        con.execute("INSERT INTO custom_expense_log (content) VALUES ('历史午饭')")
        con.execute("CREATE TABLE window_events (id INTEGER, title TEXT)")
        con.execute("CREATE INDEX idx_window_events_id ON window_events (id)")
        for i in range(500):
            con.execute("INSERT INTO window_events VALUES (?, ?)", (i, f"窗口 {i}"))
    con.close()


def make_template(root: Path) -> Path:
    """造一个底座：提示词目录 + 一个真库 + 一批「与本次无关」的历史。"""
    template = root / "base"
    (template / "agent" / "chat").mkdir(parents=True)
    (template / CP_REL).write_text("# 自定义记录规则\n", encoding="utf-8")
    (template / "prompts").mkdir()
    (template / PROMPT_REL).write_text("active_version: v1\n", encoding="utf-8")
    (template / "diary" / "2025" / "01").mkdir(parents=True)
    (template / DIARY_REL).write_text("历史日记\n", encoding="utf-8")
    (template / "user").mkdir()
    (template / USER_REL).write_text("用户画像\n", encoding="utf-8")
    make_template_db(template / DB_REL)
    return template


def empty_db(**overrides) -> DbConfig:
    """默认的库配方：只建结构 + 回填 `KEEP_TABLES`。"""
    return DbConfig(
        path=DB_REL, mode=DB_MODE_EMPTY, keep_tables=list(KEEP_TABLES), **overrides
    )


def make_config(*, extra: list[str] | None = None, db: DbConfig | None = None) -> EnvConfig:
    """一份环境配置：提示词必复制；`db` 不传 = 环境里不建库（各处以显式为准）。"""
    return EnvConfig(copy=["agent/", "prompts/"], extra=list(extra or []), db=db)


@pytest.fixture
def template(tmp_path: Path) -> Path:
    return make_template(tmp_path)


@pytest.fixture
def env_config() -> EnvConfig:
    return make_config(db=empty_db())


@pytest.fixture
def spec(template: Path) -> EnvSpec:
    """core 眼里的"环境声明"：只有一个模板目录（+ 日志用的名字）。"""
    return EnvSpec(template=template, label="测试底座")


@pytest.fixture
def provider(tmp_path: Path, env_config: EnvConfig) -> LifeprismEnvProvider:
    return LifeprismEnvProvider(
        envs_root=tmp_path / "envs", ipc_dir=tmp_path / "ipc", env_config=env_config
    )


def table_names(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {name for (name,) in con.execute("SELECT name FROM sqlite_master")}
    finally:
        con.close()


def rows_of(db_path: Path, table: str, columns: str = "*") -> list[tuple]:
    con = sqlite3.connect(db_path)
    try:
        return list(con.execute(f"SELECT {columns} FROM {table}"))
    finally:
        con.close()


# ---------------- 契约 ----------------

def test_provider_实现_envprovider_契约(provider: LifeprismEnvProvider) -> None:
    assert isinstance(provider, EnvProvider)


def test_core_的声明里只有模板与本机制无关的东西(spec: EnvSpec) -> None:
    """`EnvSpec` 只认"从哪个只读模板取初始状态"：复制清单与库配方都不在那儿。

    这是刻意的边界（见 evaluate/core/types.py 的「解耦纪律」）：core 的契约一旦认识
    "库 / 表"，换个 provider 就没人能解释这些字段，只能静默忽略。
    """
    assert set(spec.__dataclass_fields__) == {"template", "label"}


# ---------------- 造环境：只拼配置里的材料 ----------------

def test_create_env_只复制配置里的路径(provider: LifeprismEnvProvider, spec: EnvSpec) -> None:
    env = provider.create_env(spec)

    assert isinstance(env, Env)
    assert env.root.is_dir()
    assert (env.root / CP_REL).read_text(encoding="utf-8") == "# 自定义记录规则\n"
    assert (env.root / PROMPT_REL).exists()


def test_create_env_没列的路径不进环境(provider: LifeprismEnvProvider, spec: EnvSpec) -> None:
    """底座里 88% 的文件是日记这类历史数据：不列就不该出现在每条用例的环境里。"""
    env = provider.create_env(spec)

    assert not (env.root / DIARY_REL).exists()
    assert not (env.root / USER_REL).exists()


def test_create_env_extra_列了就复制(tmp_path: Path, template: Path) -> None:
    """`extra` 空 = 不复制；列了才进环境——这是「要什么」的唯一开关。"""
    provider = LifeprismEnvProvider(
        envs_root=tmp_path / "envs",
        ipc_dir=tmp_path / "ipc",
        env_config=make_config(extra=["user/"], db=empty_db()),
    )

    env = provider.create_env(EnvSpec(template=template, label="带 extra"))

    assert (env.root / USER_REL).exists()


def test_create_env_空库_结构齐而噪声为空(provider: LifeprismEnvProvider, spec: EnvSpec) -> None:
    env = provider.create_env(spec)
    db = env.root / DB_REL

    # 结构整体搬过来（表 + 索引），噪声表的行一行不留
    assert {"custom_record_types", "window_events"} <= table_names(db)
    assert "idx_window_events_id" in table_names(db)
    assert rows_of(db, "window_events") == []

    # keep_tables 的行整表回填：靠它们跑起来的用例（枚举 / 注册 / 历史基线）才有前提
    assert rows_of(db, "custom_record_types", "id") == [("t1",), ("t2",)]
    assert rows_of(db, "mood_types", "name") == [("喜悦",)]


def test_create_env_空库_自增序号跟着回填(provider: LifeprismEnvProvider, spec: EnvSpec) -> None:
    """回填了显式 id 之后，新插入的行要接着往下走，不能撞已有 id。"""
    env = provider.create_env(spec)
    db = env.root / DB_REL

    con = sqlite3.connect(db)
    with con:
        cur = con.execute("INSERT INTO custom_expense_log (content) VALUES ('新午饭')")
        new_id = cur.lastrowid
    con.close()

    assert new_id == 2


def test_create_env_整库复制模式把行也带上(tmp_path: Path, template: Path) -> None:
    provider = LifeprismEnvProvider(
        envs_root=tmp_path / "envs",
        ipc_dir=tmp_path / "ipc",
        env_config=make_config(db=DbConfig(path=DB_REL, mode=DB_MODE_COPY)),
    )
    env = provider.create_env(EnvSpec(template=template, label="整库复制"))

    assert len(rows_of(env.root / DB_REL, "window_events")) == 500


def test_create_env_不配库时环境里没有库(tmp_path: Path, template: Path) -> None:
    provider = LifeprismEnvProvider(
        envs_root=tmp_path / "envs",
        ipc_dir=tmp_path / "ipc",
        env_config=make_config(db=None),
    )
    env = provider.create_env(EnvSpec(template=template, label="不建库"))

    assert not (env.root / DB_REL).exists()


def test_create_env_每个槽位一份独立环境(provider: LifeprismEnvProvider, spec: EnvSpec) -> None:
    first = provider.create_env(spec)
    second = provider.create_env(spec)

    assert first.root != second.root
    assert first.key != second.key

    # 改第一个环境不影响底座，也不影响第二个环境
    (first.root / CP_REL).write_text("改过", encoding="utf-8")
    assert (spec.template / CP_REL).read_text(encoding="utf-8") == "# 自定义记录规则\n"
    assert (second.root / CP_REL).read_text(encoding="utf-8") == "# 自定义记录规则\n"


def test_create_env_清掉同名残留(provider: LifeprismEnvProvider, spec: EnvSpec) -> None:
    """上次 run 撞了同一个 key：先清掉再拼，不然残留会混进新环境。"""
    stale = provider.envs_root / "env01"
    stale.mkdir(parents=True)
    (stale / "残留.txt").write_text("旧的", encoding="utf-8")

    env = provider.create_env(spec)

    assert not (env.root / "残留.txt").exists()
    assert (env.root / CP_REL).exists()


def test_create_env_配置路径不存在时报错(provider: LifeprismEnvProvider, template: Path) -> None:
    """机制层也不静默跳过：少一块的环境照样能跑完，只是结果全错。"""
    broken = LifeprismEnvProvider(
        envs_root=provider.envs_root,
        ipc_dir=provider.ipc_dir,
        env_config=EnvConfig(copy=["agent/", "没有这个目录/"], extra=[], db=None),
    )

    with pytest.raises(FileNotFoundError, match="没有这个目录/"):
        broken.create_env(EnvSpec(template=template, label="坏配置"))


def test_句柄只记数据根与底座(provider: LifeprismEnvProvider, spec: EnvSpec) -> None:
    """句柄对 core 只暴露 root/key；`template` 是 provider 的内部事实（reset 用它）。"""
    env = provider.create_env(spec)

    assert env.template == spec.template


# ---------------- 重置 ----------------

def test_reset_env_整份回滚(provider: LifeprismEnvProvider, spec: EnvSpec) -> None:
    """改坏的、新加的、被删的，全部回到配置态（文件与库两边都要回）。

    这是槽位能复用的唯一依据：用例之间不靠「哪些文件会变」的清单，靠整体回滚——
    清单永远会漏，漏了还不报错（下一条用例会继承上一条的写入）。
    """
    env = provider.create_env(spec)
    (env.root / CP_REL).write_text("被上一条用例改坏", encoding="utf-8")
    (env.root / "new.md").write_text("上一条用例新建的", encoding="utf-8")
    (env.root / PROMPT_REL).unlink()
    con = sqlite3.connect(env.root / DB_REL)
    with con:
        con.execute("INSERT INTO window_events VALUES (9999, '上一条用例写的')")
    con.close()

    provider.reset_env(env)

    assert (env.root / CP_REL).read_text(encoding="utf-8") == "# 自定义记录规则\n"
    assert (env.root / PROMPT_REL).exists()
    assert not (env.root / "new.md").exists()
    assert rows_of(env.root / DB_REL, "window_events") == []
    assert rows_of(env.root / DB_REL, "custom_record_types", "id") == [("t1",), ("t2",)]
    assert env.key == "env01"  # 重置不改 key：还是同一个槽位


def test_reset_env_不认识的环境类型直接报错(provider: LifeprismEnvProvider, spec: EnvSpec) -> None:
    class Other(Env):
        @property
        def root(self) -> Path:  # pragma: no cover - 只为造一个异类
            return Path(".")

        @property
        def key(self) -> str:  # pragma: no cover
            return "other"

    with pytest.raises(AssertionError):
        provider.reset_env(Other())  # type: ignore[arg-type]


def test_dispose_env_删掉环境目录(provider: LifeprismEnvProvider, spec: EnvSpec) -> None:
    env = provider.create_env(spec)
    provider.dispose_env(env)

    assert not env.root.exists()
    assert env.root.parent.is_dir()  # 只删自己，不删 envs 根


# ---------------- 执行通道 ----------------

def test_create_worker_给出绑定了该环境的子进程通道(
    provider: LifeprismEnvProvider, spec: EnvSpec
) -> None:
    env = provider.create_env(spec)
    worker = provider.create_worker(env)

    assert isinstance(worker, SubprocessWorker)


# ---------------- 开槽前自检 ----------------

def test_check_env_inputs_正常配置通过(template: Path, env_config: EnvConfig) -> None:
    check_env_inputs(template, env_config)  # 不抛就是过


def test_check_env_inputs_路径不在底座里就报错(template: Path, env_config: EnvConfig) -> None:
    check_env_inputs(template, env_config)  # 先确认前提成立

    with pytest.raises(FileNotFoundError, match="不存在的.md"):
        check_env_inputs(
            template,
            EnvConfig(copy=["agent/chat/不存在的.md"], extra=[], db=env_config.db),
        )


def test_check_env_inputs_库不在底座里就报错(template: Path, env_config: EnvConfig) -> None:
    with pytest.raises(FileNotFoundError, match="dataset/other.db"):
        check_env_inputs(
            template,
            EnvConfig(
                copy=["agent/", "prompts/"],
                extra=[],
                db=DbConfig(path="dataset/other.db", mode=DB_MODE_EMPTY),
            ),
        )


def test_check_env_inputs_keep_tables_写错表名就报错(
    template: Path, env_config: EnvConfig
) -> None:
    """表名打错、底座换代导致表没了，都要在跑之前拦住（不是等某条用例莫名失败）。"""
    with pytest.raises(ValueError, match="memories"):
        check_env_inputs(
            template,
            EnvConfig(
                copy=["agent/", "prompts/"],
                extra=[],
                db=DbConfig(path=DB_REL, mode=DB_MODE_EMPTY, keep_tables=["memories"]),
            ),
        )


def test_check_env_inputs_提示词必须覆盖到(template: Path, env_config: EnvConfig) -> None:
    """提示词是环境的必需项：漏了它 agent 起不来，或起得来但跑的不是记录的那一版。"""
    with pytest.raises(ValueError, match="prompts/agent_prompts.yaml"):
        check_env_inputs(
            template, EnvConfig(copy=["agent/"], extra=[], db=env_config.db)
        )

    # 直接列那个文件也算覆盖
    check_env_inputs(
        template, EnvConfig(copy=["agent/", PROMPT_BOOK_REL], extra=[], db=env_config.db)
    )


def test_check_env_inputs_复制清单不能为空(template: Path, env_config: EnvConfig) -> None:
    """空清单 = 造一个没有提示词的环境：用例全挂却看着像被测对象的问题。"""
    with pytest.raises(ValueError, match="copy"):
        check_env_inputs(template, EnvConfig(copy=[], extra=[], db=env_config.db))


# ---------------- 配置自身的校验 ----------------

def test_db_config_取值非法时报错() -> None:
    with pytest.raises(ValueError, match="mode"):
        DbConfig(path=DB_REL, mode="half")

    with pytest.raises(ValueError, match="keep_tables"):
        # 整库都复制了，keep_tables 写了也不会生效——宁可报错，不要「看着像生效了」
        DbConfig(path=DB_REL, mode=DB_MODE_COPY, keep_tables=["mood_types"])
