"""环境机制的语义：造 / 整份回滚 / 销毁 / 出执行通道。

这三条是槽位池能成立的前提（见 evaluate/core/types.py 的 EnvProvider 契约），
尤其「整份回滚」——它替代了上一版「按可变路径清单还原」的做法，判据从「猜哪些会变」
变成「整体回基线」。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from evaluate.core import Env, EnvProvider, EnvSpec

from lifeprismevalue.evalue.env import LifeprismEnv, LifeprismEnvProvider
from lifeprismevalue.evalue.worker import SubprocessWorker


def make_template(root: Path) -> Path:
    """造一个极小的底座模板（三个文件 + 一层子目录）。"""
    template = root / "base"
    (template / "sub").mkdir(parents=True)
    (template / "a.txt").write_text("A", encoding="utf-8")
    (template / "sub" / "b.txt").write_text("B", encoding="utf-8")
    return template


@pytest.fixture
def provider(tmp_path: Path) -> LifeprismEnvProvider:
    return LifeprismEnvProvider(envs_root=tmp_path / "envs", ipc_dir=tmp_path / "ipc")


@pytest.fixture
def spec(tmp_path: Path) -> EnvSpec:
    return EnvSpec(template=make_template(tmp_path), label="测试底座")


def test_provider_实现_envprovider_契约(provider: LifeprismEnvProvider) -> None:
    assert isinstance(provider, EnvProvider)


def test_create_env_整棵复制底座(provider, spec) -> None:
    env = provider.create_env(spec)

    assert isinstance(env, Env)
    assert env.root.is_dir()
    assert (env.root / "a.txt").read_text(encoding="utf-8") == "A"
    assert (env.root / "sub" / "b.txt").read_text(encoding="utf-8") == "B"
    assert env.template == spec.template


def test_create_env_每个槽位一份独立环境(provider, spec) -> None:
    first = provider.create_env(spec)
    second = provider.create_env(spec)

    assert first.root != second.root
    assert first.key != second.key

    # 改第一个环境不影响底座，也不影响第二个环境
    (first.root / "a.txt").write_text("改过", encoding="utf-8")
    assert (spec.template / "a.txt").read_text(encoding="utf-8") == "A"
    assert (second.root / "a.txt").read_text(encoding="utf-8") == "A"


def test_reset_env_整份回滚(provider, spec) -> None:
    """改坏的、新加的、被删的，全部回到基线。

    这是槽位能复用的唯一依据：用例之间不靠「哪些文件会变」的清单，靠整体回滚——
    清单永远会漏，漏了还不报错（下一条用例会继承上一条的写入）。
    """
    env = provider.create_env(spec)
    (env.root / "a.txt").write_text("被上一条用例改坏", encoding="utf-8")
    (env.root / "new.md").write_text("上一条用例新建的", encoding="utf-8")
    (env.root / "sub" / "b.txt").unlink()

    provider.reset_env(env)

    assert (env.root / "a.txt").read_text(encoding="utf-8") == "A"
    assert (env.root / "sub" / "b.txt").read_text(encoding="utf-8") == "B"
    assert not (env.root / "new.md").exists()
    assert env.key == "env01"  # 重置不改 key：还是同一个槽位


def test_reset_env_不认识的环境类型直接报错(provider, spec) -> None:
    class Other(Env):
        @property
        def root(self) -> Path:  # pragma: no cover - 只为造一个异类
            return Path(".")

        @property
        def key(self) -> str:  # pragma: no cover
            return "other"

    with pytest.raises(AssertionError):
        provider.reset_env(Other())  # type: ignore[arg-type]


def test_dispose_env_删掉环境目录(provider, spec) -> None:
    env = provider.create_env(spec)
    provider.dispose_env(env)

    assert not env.root.exists()
    assert env.root.parent.is_dir()  # 只删自己，不删 envs 根


def test_create_worker_给出绑定了该环境的子进程通道(provider, spec) -> None:
    env = provider.create_env(spec)
    worker = provider.create_worker(env)

    assert isinstance(worker, SubprocessWorker)


def test_create_env_清掉同名残留(provider, spec) -> None:
    """上次 run 撞了同一个 key：先清掉再复制，不然 copytree 会直接报错。"""
    stale = provider.envs_root / "env01"
    stale.mkdir(parents=True)
    (stale / "残留.txt").write_text("旧的", encoding="utf-8")

    env = provider.create_env(spec)

    assert not (env.root / "残留.txt").exists()
    assert (env.root / "a.txt").exists()
