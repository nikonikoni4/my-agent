"""环境验收（行为级）：按**真配置**从**真底座**拼出的环境，工具层真的能跑通吗？

**为什么需要这一层**：其余测试只看"结构与行数"（表建出来了吗、keep_tables 回填了吗），
而行数对**不等于**工具跑得通。真实教训：`keep_tables` 曾漏掉 `mood_impacts`（影响因素枚举）
与 `habit_challenges`（进行中的挑战）——前者让「命中枚举」的 rubric 失去依据，后者让
`checkin_today` 直接抛 `NotFoundError`。两类都表现为「agent 记错了」，而根因是环境缺一块，
**且不报错**。这一层就是拦它的：把用例依赖的动作真的跑一遍。

**怎么做到不调 LLM**：切数据根（子进程里 `case.py` 做的第一件事）之后，直接调工具背后的
数据层入口（`data/`）——那就是工具真正走的那段代码。

**跳过条件**：需要本机的 `lifeprismTestData/`（被 `.gitignore` 忽略，只有开发机上有）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest
from evaluate.core import EnvSpec

from lifeprismevalue import config, db
from lifeprismevalue.data import behavior, custom_records, habits, mood
from lifeprismevalue.evalue.caseload import load_case_set
from lifeprismevalue.evalue.env import LifeprismEnv, LifeprismEnvProvider
from lifeprismevalue.utils.time_utils import get_local_today

# src/lifeprismevalue/evalue/tests/ 往上四层是仓库根
REPO_ROOT = Path(__file__).resolve().parents[4]
BASE_DIR = REPO_ROOT / "lifeprismTestData" / "base"
CASES_PATH = REPO_ROOT / "lifeprismTestData" / "defs" / "记录任务-01" / "cases.yaml"

pytestmark = pytest.mark.skipif(
    not (BASE_DIR.is_dir() and CASES_PATH.is_file()),
    reason="需要真底座 lifeprismTestData/base 与用例文件（被 .gitignore 忽略，只有开发机上有）",
)


@dataclass
class Built:
    """一次验收用的环境与它的机制（provider 留着做 reset / dispose）。"""

    provider: LifeprismEnvProvider
    env: LifeprismEnv


@pytest.fixture
def built(tmp_path: Path) -> Iterator[Built]:
    """按真配置造一份环境，并把数据根切过去；用完还原数据根、销毁环境。

    `config.use_data_path` 是**进程级全局**：不还原就会把同进程里其它测试的数据根
    也带偏（那种污染表现为"别的测试莫名读到空库"，很难定位）。
    """
    case_set = load_case_set(CASES_PATH)
    provider = LifeprismEnvProvider(
        envs_root=tmp_path / "envs", ipc_dir=tmp_path / "ipc", env_config=case_set.meta.env
    )
    env = provider.create_env(EnvSpec(template=BASE_DIR, label=case_set.meta.id))

    saved = config.get_lifeprism_data_path()
    config.use_data_path(env.root)
    try:
        yield Built(provider=provider, env=env)
    finally:
        config.use_data_path(saved)
        provider.dispose_env(env)


def checkin_rows() -> int:
    """环境库里当前有多少条打卡记录（用来判 reset 是否真的回滚了库）。"""
    return len(db.query_all("SELECT id FROM habit_checkins"))


def test_工具层在真环境里跑得通用例依赖的全部动作(built: Built) -> None:
    """把各类记录用例依赖的动作各跑一遍（工具背后那段代码，不需要 LLM）。

    每一条都对应一类用例；任何一条抛异常，都说明这份环境配置对那类用例不够用。
    """
    # 1.1 支出记录：注册表得读得到类型，写进去要能落库
    types = {t["slug"]: t for t in custom_records.list_types()}
    assert {"expense_log", "budget_pool_balance"} <= set(types), (
        f"custom_record_types 不完整，读到 {sorted(types)}"
    )
    custom_records.create_entry(
        types["expense_log"]["id"],
        {"amount": 15.3, "content": "午饭", "expense_category": "餐饮"},
    )

    # 1.1 的 rubric 是「生存池较此前减少 N」：环境里必须真有「此前」
    latest = db.query_one(
        "SELECT survival_pool_balance FROM custom_budget_pool_balance "
        "ORDER BY created_at DESC LIMIT 1"
    )
    assert latest is not None, "custom_budget_pool_balance 没有历史基线，rubric 里的「此前」不存在"
    custom_records.create_entry(
        types["budget_pool_balance"]["id"],
        {"month": "2026-09", "survival_pool_balance": latest["survival_pool_balance"] - 15.3,
         "change_note": "午饭15.3元"},
    )

    # 1.6 心情记录：枚举必须读得到（rubric 要求「命中喜悦」、影响因素「含健康」）
    mood_names = [t["name"] for t in mood.get_mood_types()]
    impact_names = [i["name"] for i in mood.get_mood_impacts()]
    assert "喜悦" in mood_names, f"mood_types 缺枚举「喜悦」：{mood_names}"
    assert "健康" in impact_names, f"mood_impacts 缺枚举「健康」：{impact_names}"
    mood.create_mood_entry({"mood_type_id": "joy", "score": 90, "content": "开心"})

    # 1.7 习惯打卡：先查到习惯、再拿到「进行中的挑战」才可能打卡成功
    habit = next((h for h in habits.get_habits() if h["name"] == "每日锻炼"), None)
    assert habit is not None, "habits 里没有「每日锻炼」，打卡类用例无法执行"
    assert habits.get_current_challenge(habit["id"]) is not None, (
        "「每日锻炼」没有进行中的挑战：checkin_today 会抛 NotFoundError（当前无进行中的挑战）"
    )
    response = habits.get_habit_service().checkin_today(habit["id"])
    assert response.checkin.date == get_local_today().isoformat()

    # 1.8 时间块备注
    block = behavior.create_custom_block(
        {"start_time": "2026-09-17T19:00:00+08:00", "end_time": "2026-09-17T20:00:00+08:00",
         "content": "公园散步", "duration": 60, "color": "#3B82F6"}
    )
    assert block, "时间块写入没有返回记录"

    # 1.9 日记：文件类，工具直接往数据根写文件（父目录不存在时自己建）
    today = get_local_today()
    diary = built.env.root / f"diary/{today:%Y/%m}/{today:%Y-%m-%d}.md"
    diary.parent.mkdir(parents=True, exist_ok=True)
    diary.write_text("上午写代码\n", encoding="utf-8")
    assert diary.is_file()


def test_槽位复用后仍能跑通_含库的回滚(built: Built) -> None:
    """reset 之后环境必须回到初始态——**包括库**，不只是文件。

    判据取「打卡第二次还能成功」：`habit_checkins` 上 (habit_id, date) 唯一，上一条用例的
    打卡若没被回滚，这一条会因「今日已打卡」抛 ConflictError。这比"数行数相等"更有力：
    它验的正是「下一条用例不会继承上一条的写入」这条核心契约。
    """
    habit = next(h for h in habits.get_habits() if h["name"] == "每日读书")
    before = checkin_rows()

    habits.get_habit_service().checkin_today(habit["id"])
    assert checkin_rows() == before + 1

    built.provider.reset_env(built.env)

    assert checkin_rows() == before, "reset 没有把 habit_checkins 回滚回初始态"
    assert habits.get_habit_service().checkin_today(habit["id"]).checkin.date == (
        get_local_today().isoformat()
    )
