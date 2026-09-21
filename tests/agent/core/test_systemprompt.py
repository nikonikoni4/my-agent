"""SystemPrompt 组装器的行为测试。

规格来源：systemprompt.py 头部注释的三条设计意图
1. 隔离机制：agent_name -> prompt 的显式注册
2. 遮蔽机制：agent 层同名 section 顶替全球层
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

def test_register_section_to_agent(sp):
    """测试场景：agent 注册的 section 出现在该 agent 的组装结果中"""
    sp.register_section("coder", make_section("tool:bash", 100, "bash 指导"))
    assembly = sp.assemble("coder")
    names = [s.name for s in assembly.sections.values()]
    assert "tool:bash" in names
    assert "identity" in names  # 全球段仍存在


def test_agent_section_shadows_global(sp):
    """测试场景：agent 注册与全球同名的 section，遮蔽后只出现 agent 版本"""
    sp.register_section(
        "coder",
        make_section("identity", -100, "你是代码审查员"),
    )
    assembly = sp.assemble("coder")
    identity_sections = [s for s in assembly.sections.values() if s.name == "identity"]
    assert len(identity_sections) == 1, "同名段遮蔽后不应重复出现"
    assert identity_sections[0].text == "你是代码审查员"


def test_sections_isolated_between_agents(sp):
    """测试场景：agent A 注册的段不出现在 agent B 的组装结果中"""
    sp.register_section("coder", make_section("tool:bash", 100, "bash 指导"))
    coder_names = [s.name for s in sp.assemble("coder").sections.values()]
    writer_names = [s.name for s in sp.assemble("writer").sections.values()]
    assert "tool:bash" in coder_names
    assert "tool:bash" not in writer_names


# ---------- 排序 ----------

def test_sorted_section_by_order(sp):
    """测试场景：组装结果的段按 order 升序排列"""
    sp.register_section("coder", make_section("tool:bash", 100, "b"))
    sp.register_section("coder", make_section("policy", 50, "p"))
    sp.register_section("coder", make_section("persona", 0, "u"))
    orders = [s.order for s in sp.assemble("coder").sorted_sections()]
    assert orders == sorted(orders)


# ---------- context（快照路） ----------

def test_register_context_into_assembly(sp):
    """测试场景：register_context 的内容出现在 assembly.context 中"""
    sp.register_context("coder", "当前时间: 2026-09-07")
    assembly = sp.assemble("coder")
    assert "当前时间: 2026-09-07" in assembly.context


def test_context_isolated_between_agents(sp):
    """测试场景：agent A 的 context 不出现在 agent B 的组装结果中"""
    sp.register_context("coder", "终端位置: /home/coder")
    assert "终端位置" not in sp.assemble("writer").context


# ---------- 带类型标注的 context（System Reminder / Runtime） ----------

def test_agent_context_stored_as_typed_items(sp):
    """测试场景：注册的上下文以 ContextItem 存储并带正确类型标注"""
    sp.register_context("coder", "运行时标注")
    sp.register_system_reminder("coder", "文件读取的提醒")
    items = sp._agent_context["coder"]
    assert all(isinstance(it, ContextItem) for it in items)
    assert [it.context_type for it in items] == [
        ContextType.RUNTIME,
        ContextType.SYSTEM_REMINDER,
    ]


def test_runtime_and_system_reminder_split_in_assembly(sp):
    """测试场景：assemble 将 runtime 与 system reminder 分别落到两个字段"""
    sp.register_context("coder", "当前时间: 2026-09-07")
    sp.register_system_reminder("coder", "请遵守编码规范")
    assembly = sp.assemble("coder")
    assert "当前时间: 2026-09-07" in assembly.context
    assert "请遵守编码规范" not in assembly.context
    assert "请遵守编码规范" in assembly.system_reminder
    assert "当前时间: 2026-09-07" not in assembly.system_reminder


def test_multiple_contexts_joined_by_newline(sp):
    """测试场景：同类多条上下文按换行拼接，保持注册顺序"""
    sp.register_context("coder", "第一条")
    sp.register_context("coder", "第二条")
    sp.register_system_reminder("coder", "提醒一")
    sp.register_system_reminder("coder", "提醒二")
    assembly = sp.assemble("coder")
    assert assembly.context == "第一条\n第二条"
    assert assembly.system_reminder == "提醒一\n提醒二"


def test_system_reminder_isolated_between_agents(sp):
    """测试场景：agent A 的 system reminder 不出现在 agent B 的组装结果中"""
    sp.register_system_reminder("coder", "只给 coder 的提醒")
    assert "只给 coder 的提醒" not in sp.assemble("writer").system_reminder


def test_unregister_removes_system_reminder(sp):
    """测试场景：注销后 system reminder 一并清除"""
    sp.register_system_reminder("coder", "待清除提醒")
    sp.unregister("coder")
    assert "待清除提醒" not in sp.assemble("coder").system_reminder


# ---------- 上下文条目的参数注入 ----------

def test_reminder_callback_renders_with_params(sp):
    """测试场景：reminder 注册为 callback 时，按其 name 取参数注入后进消息面"""
    sp.register_system_reminder(
        "coder",
        lambda data_path: f"规则文件在 {data_path}/agent/chat/rules.md",
        name="custom_prompt",
    )
    assembly = sp.assemble("coder", {"custom_prompt": {"data_path": "/srv/data"}})
    assert assembly.system_reminder == "规则文件在 /srv/data/agent/chat/rules.md"


def test_reminder_callback_without_params_is_skipped(sp):
    """测试场景：callback reminder 缺注入参数时跳过该条，不影响其他条目"""
    sp.register_system_reminder("coder", lambda data_path: f"渲染了{data_path}", name="custom_prompt")
    sp.register_system_reminder("coder", "静态提醒")
    assert sp.assemble("coder").system_reminder == "静态提醒"


def test_reminder_str_ignores_params(sp):
    """测试场景：str reminder 不吃参数，给了参数也原样输出（与 section 同约定）"""
    sp.register_system_reminder("coder", "带 {data_path} 的原文")
    assembly = sp.assemble("coder", {"custom_prompt": {"data_path": "/srv"}})
    assert assembly.system_reminder == "带 {data_path} 的原文"


def test_context_callback_renders_with_params(sp):
    """测试场景：runtime-context 与 reminder 走同一条注入约定，不是只管 reminder"""
    sp.register_context("coder", lambda now: f"当前时间: {now}", name="runtime")
    assembly = sp.assemble("coder", {"runtime": {"now": "2026-09-21"}})
    assert assembly.context == "当前时间: 2026-09-21"


# ---------- 注销 ----------

def test_unregister_removes_agent_content(sp):
    """测试场景：注销后该 agent 的段与 context 全部消失，回落全球层"""
    sp.register_section("coder", make_section("tool:bash", 100, "bash 指导"))
    sp.register_context("coder", "当前时间: 2026-09-07")
    sp.unregister("coder")
    names = [s.name for s in sp.assemble("coder").sections.values()]
    assert "tool:bash" not in names
    assert "identity" in names
    assert "当前时间" not in sp.assemble("coder").context


def test_unregister_unknown_agent_does_not_raise(sp):
    """测试场景：注销未注册的 agent 不应抛错"""
    sp.unregister("ghost")


# ---------- 渲染 ----------

def test_render_static_sections_joined(sp):
    """测试场景：静态段按 order 排序后拼接成字符串"""
    sp.register_section("coder", make_section("tool:bash", 100, "bash 指导"))
    sp.register_section("coder", make_section("persona", 0, "你是一个编码助手"))
    assembly = sp.assemble("coder")
    text = SystemPrompt.render(assembly)
    assert "你是一个个人助手" in text   # identity(-100)
    assert "你是一个编码助手" in text   # persona(0)
    assert "bash 指导" in text         # tool:bash(100)
    assert text.index("你是一个个人助手") < text.index("你是一个编码助手") < text.index("bash 指导")


def test_render_callable_section_with_params(sp):
    """测试场景：text 为可调用对象时，按段名从 params 取参数注入"""
    sp.register_section(
        "coder",
        make_section("deployment", 0, lambda *, cwd: f"工作目录: {cwd}"),
    )
    assembly = sp.assemble("coder")
    text = SystemPrompt.render(assembly, params={"deployment": {"cwd": "/home/u"}})
    assert "工作目录: /home/u" in text


def test_render_callable_without_params_skipped(caplog):
    """测试场景：text 为可调用对象但无注入参数时，跳过该段并告警，不抛错"""
    sp = SystemPrompt()
    sp.register_section("coder", make_section("deployment", 0, lambda *, cwd: cwd))
    assembly = sp.assemble("coder")
    with caplog.at_level(logging.WARNING):
        text = SystemPrompt.render(assembly, params={})
    assert "工作目录" not in text
    assert any("deployment" in r.message for r in caplog.records)


def test_render_static_with_params_renders_text(sp):
    """测试场景：静态段的 text 不做参数注入，即使 params 里带同名键也正常输出"""
    sp.register_section("coder", make_section("identity", -100, "固定文本"))
    assembly = sp.assemble("coder")
    text = SystemPrompt.render(assembly, params={"identity": {"cwd": "/x"}})
    assert "固定文本" in text
