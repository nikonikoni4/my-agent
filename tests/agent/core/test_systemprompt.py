"""SystemPrompt 组装器的行为测试。

规格来源：systemprompt.py 头部注释的三条设计意图
1. 隔离机制：一个实例只服务一个 agent，跨 agent 隔离由持有方各建一份实例保证
   （组件内部不再有 agent_name 这一维，实例本身就是作用域）
2. 遮蔽机制：注册的 section 与全局默认同名时，全局的该 section 被顶替
3. 动态变化：context 走快照路（不进 system 文本）
"""

import logging

import pytest

from myagent.agent.core.systemprompt.systemprompt import SystemPrompt
from myagent.agent.core.systemprompt.types import ContextItem, ContextType, PrompSection


def make_section(name: str, order: int, text: str) -> PrompSection:
    return PrompSection(name=name, order=order, text=text)


@pytest.fixture
def sp() -> SystemPrompt:
    return SystemPrompt()


# ---------- 全球层默认内容 ----------

def test_default_global_prompt(sp):
    """测试场景：构造后全球层应包含 identity 段，order 为 -100"""
    assert "identity" in sp._global_prompt
    assert sp._global_prompt["identity"].order == -100


# ---------- 注册与遮蔽 ----------

def test_register_section_appears_in_assembly(sp):
    """测试场景：注册的 section 出现在组装结果中"""
    sp.register_section(make_section("tool:bash", 100, "bash 指导"))
    assembly = sp.assemble()
    names = [s.name for s in assembly.sections.values()]
    assert "tool:bash" in names
    assert "identity" in names  # 全球段仍存在


def test_registered_section_shadows_global(sp):
    """测试场景：注册与全球同名的 section，遮蔽后只出现注册版本"""
    sp.register_section(make_section("identity", -100, "你是代码审查员"))
    assembly = sp.assemble()
    identity_sections = [s for s in assembly.sections.values() if s.name == "identity"]
    assert len(identity_sections) == 1, "同名段遮蔽后不应重复出现"
    assert identity_sections[0].text == "你是代码审查员"


def test_两个实例的注册互不可见(sp):
    """测试场景：隔离由"一个实例一个 agent"保证——A 实例注册的段、runtime context
    与 system reminder 都不出现在 B 实例的组装结果中"""
    other = SystemPrompt()
    sp.register_section(make_section("tool:bash", 100, "bash 指导"))
    sp.register_context("终端位置: /home/coder")
    sp.register_system_reminder("只给 A 的提醒")

    a = sp.assemble()
    b = other.assemble()

    assert "tool:bash" in [s.name for s in a.sections.values()]
    assert "tool:bash" not in [s.name for s in b.sections.values()]
    assert "终端位置" in a.context and "终端位置" not in b.context
    assert "只给 A 的提醒" in a.system_reminder and "只给 A 的提醒" not in b.system_reminder


# ---------- 排序 ----------

def test_sorted_section_by_order(sp):
    """测试场景：组装结果的段按 order 升序排列"""
    sp.register_section(make_section("tool:bash", 100, "b"))
    sp.register_section(make_section("policy", 50, "p"))
    sp.register_section(make_section("persona", 0, "u"))
    orders = [s.order for s in sp.assemble().sorted_sections()]
    assert orders == sorted(orders)


# ---------- context（快照路） ----------

def test_register_context_into_assembly(sp):
    """测试场景：register_context 的内容出现在 assembly.context 中"""
    sp.register_context("当前时间: 2026-09-07")
    assembly = sp.assemble()
    assert "当前时间: 2026-09-07" in assembly.context


# ---------- 带类型标注的 context（System Reminder / Runtime） ----------

def test_context_stored_as_typed_items(sp):
    """测试场景：注册的上下文以 ContextItem 存储并带正确类型标注"""
    sp.register_context("运行时标注")
    sp.register_system_reminder("文件读取的提醒")
    items = sp._context
    assert all(isinstance(it, ContextItem) for it in items)
    assert [it.context_type for it in items] == [
        ContextType.RUNTIME,
        ContextType.SYSTEM_REMINDER,
    ]


def test_runtime_and_system_reminder_split_in_assembly(sp):
    """测试场景：assemble 将 runtime 与 system reminder 分别落到两个字段"""
    sp.register_context("当前时间: 2026-09-07")
    sp.register_system_reminder("请遵守编码规范")
    assembly = sp.assemble()
    assert "当前时间: 2026-09-07" in assembly.context
    assert "请遵守编码规范" not in assembly.context
    assert "请遵守编码规范" in assembly.system_reminder
    assert "当前时间: 2026-09-07" not in assembly.system_reminder


def test_multiple_contexts_joined_by_newline(sp):
    """测试场景：同类多条上下文按换行拼接，保持注册顺序"""
    sp.register_context("第一条")
    sp.register_context("第二条")
    sp.register_system_reminder("提醒一")
    sp.register_system_reminder("提醒二")
    assembly = sp.assemble()
    assert assembly.context == "第一条\n第二条"
    assert assembly.system_reminder == "提醒一\n提醒二"


# ---------- 清空 ----------

def test_clear_removes_registered_content(sp):
    """测试场景：清空后注册的段与 context 全部消失，回落全球层"""
    sp.register_section(make_section("tool:bash", 100, "bash 指导"))
    sp.register_context("当前时间: 2026-09-07")
    sp.register_system_reminder("待清除提醒")
    sp.clear()
    assembly = sp.assemble()
    names = [s.name for s in assembly.sections.values()]
    assert "tool:bash" not in names
    assert "identity" in names
    assert "当前时间" not in assembly.context
    assert "待清除提醒" not in assembly.system_reminder


def test_clear_is_idempotent(sp):
    """测试场景：清空是幂等的——未注册任何内容时调用、连续调用两次都不抛错"""
    sp.clear()
    sp.clear()


# ---------- 上下文条目的参数注入 ----------

def test_reminder_callback_renders_with_params(sp):
    """测试场景：reminder 注册为 callback 时，按其 name 取参数注入后进消息面"""
    sp.register_system_reminder(
        lambda data_path: f"规则文件在 {data_path}/agent/chat/rules.md",
        name="custom_prompt",
    )
    assembly = sp.assemble({"custom_prompt": {"data_path": "/srv/data"}})
    assert assembly.system_reminder == "规则文件在 /srv/data/agent/chat/rules.md"


def test_reminder_callback_without_params_is_skipped(sp):
    """测试场景：callback reminder 缺注入参数时跳过该条，不影响其他条目"""
    sp.register_system_reminder(lambda data_path: f"渲染了{data_path}", name="custom_prompt")
    sp.register_system_reminder("静态提醒")
    assert sp.assemble().system_reminder == "静态提醒"


def test_reminder_str_ignores_params(sp):
    """测试场景：str reminder 不吃参数，给了参数也原样输出（与 section 同约定）"""
    sp.register_system_reminder("带 {data_path} 的原文")
    assembly = sp.assemble({"custom_prompt": {"data_path": "/srv"}})
    assert assembly.system_reminder == "带 {data_path} 的原文"


def test_context_callback_renders_with_params(sp):
    """测试场景：runtime-context 与 reminder 走同一条注入约定，不是只管 reminder"""
    sp.register_context(lambda now: f"当前时间: {now}", name="runtime")
    assembly = sp.assemble({"runtime": {"now": "2026-09-21"}})
    assert assembly.context == "当前时间: 2026-09-21"


# ---------- 渲染 ----------

def test_render_static_sections_joined(sp):
    """测试场景：静态段按 order 排序后拼接成字符串"""
    sp.register_section(make_section("tool:bash", 100, "bash 指导"))
    sp.register_section(make_section("persona", 0, "你是一个编码助手"))
    assembly = sp.assemble()
    text = SystemPrompt.render(assembly)
    assert "你是一个个人助手" in text   # identity(-100)
    assert "你是一个编码助手" in text   # persona(0)
    assert "bash 指导" in text         # tool:bash(100)
    assert text.index("你是一个个人助手") < text.index("你是一个编码助手") < text.index("bash 指导")


def test_render_callable_section_with_params(sp):
    """测试场景：text 为可调用对象时，按段名从 params 取参数注入"""
    sp.register_section(make_section("deployment", 0, lambda *, cwd: f"工作目录: {cwd}"))
    assembly = sp.assemble()
    text = SystemPrompt.render(assembly, params={"deployment": {"cwd": "/home/u"}})
    assert "工作目录: /home/u" in text


def test_render_callable_without_params_skipped(caplog):
    """测试场景：text 为可调用对象但无注入参数时，跳过该段并告警，不抛错"""
    sp = SystemPrompt()
    sp.register_section(make_section("deployment", 0, lambda *, cwd: cwd))
    assembly = sp.assemble()
    with caplog.at_level(logging.WARNING):
        text = SystemPrompt.render(assembly, params={})
    assert "工作目录" not in text
    assert any("deployment" in r.message for r in caplog.records)


def test_render_static_with_params_renders_text(sp):
    """测试场景：静态段的 text 不做参数注入，即使 params 里带同名键也正常输出"""
    sp.register_section(make_section("identity", -100, "固定文本"))
    assembly = sp.assemble()
    text = SystemPrompt.render(assembly, params={"identity": {"cwd": "/x"}})
    assert "固定文本" in text
