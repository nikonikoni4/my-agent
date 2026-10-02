"""模拟用户 agent（评测的"输入方"）。

角色：在 `input_mode: agent` 的用例里扮演用户，与 under_test agent 对话，
驱动它完成用例目标（`simulator.goal` / `known_facts`）。

现状：骨架（会话 / 事件 / 重试 / LLM）已接好，并把 `Simulator_prompt` 注册为 System Prompt。
后续：角色提示词改为从 `lifeprismTestData/defs/agents/simulator.md` 加载；
      本角色不记录数据，故不注册记录类工具。
"""

from __future__ import annotations

from pathlib import Path

from myagent.agent.agent_context import AgentContext, AgentPolicySpec
from myagent.agent.core.agent.types import AgentConfig
from myagent.agent.core.session import Session, SessionStore
from myagent.agent.core.systemprompt import PrompSection, SystemPrompt
from myagent.agent.llm.llm_retry import LLMRerty
from myagent.agent.llm.openai_provider import OpenAIProvider
from myagent.config.system_config import get_llm_api_key, get_llm_base_url, get_llm_model
from myagent.infra.events.eventspec import REQUEST_ERROR

from lifeprismevalue.config import get_lifeprism_data_path

AGENT_NAME = "simulator"
# 会话落盘根目录（gitignored）；lifeprism 数据目录编码为其下的一层项目子目录
SESSION_FOLDER = Path("localData")

Simulator_prompt = """
# role
你是一个agent测评助手,你需要模拟user消息，灵活的应对被评估的agent的输出，使其能够完成目标。

# 任务说明
当前是一个agent评估任务，由于agent输出不稳定，再多轮对话中只能由另一个agent（你）作为user指挥被评估的agent完成任务。
当接收到“测评任务”开头的消息之后，你的每轮输出都会作为user消息传入被评估agent。
例如：
- 发给你的消息：“测评开始，接下来你需要模拟user消息与被评估agent对话，使得被评估的agent完成下面的要求：让评估agnet完整记录xxx”

# rule
1. 接收到“测评任务”的消息之后，你必须以user的角色进行输出，并严格遵循后面的「用户风格」段
2. 只发起对话所需的内容，不要罗列要求清单、不要教被评估 agent 怎么做、不要提“目标/要求”
3. 当你认为被评估的agent任务完成之后，只输出:“测评完成”，系统将会检测你的输出来判定测评是否完成
"""

# 默认用户风格：单独成段（section），可通过 create_simulator_agent(style=...) 覆盖。
# 目标是贴近真实用户口吻，样例见 lifeprismData/session（可用 show_messages.py --role user 查看）。
DEFAULT_USER_STYLE = """\
# 用户风格
模仿真实用户的说话方式，务必贴近：
- 短、口语、第一人称，直接说自己做的事；例：“记录午饭，15.3元”“今天做了20分钟平板支撑和臀桥”“昨天没睡好”
- 不用“请你 / 请按 / 需要包含…”这类指令腔，不写 1./2./3. 清单，不做任务分解，不教对方怎么做
- 一次只给当下想到的信息，不必一次给全；缺的信息留给对方来问，不主动罗列细节
- 不复述“目标 / 要求”，不解释自己为什么这么说
"""


def register_simulator_system_prompt(
    system_prompt: SystemPrompt,
    style: str | None = DEFAULT_USER_STYLE,
) -> None:
    """把模拟用户 agent 的提示词段注册进传入的 SystemPrompt（工厂传 `ctx.system_prompt`）。

    两个 section：
    - `identity`：角色与规则（`Simulator_prompt`）。注册名用 `identity` 是为了遮蔽
      全局默认的「你是一个个人助手…」，避免与「模拟用户」角色冲突。
    - `user_style`：用户风格（默认 `DEFAULT_USER_STYLE`；传 None 则不注册）。
      风格单独成段，便于按用例切换（将来可由 case 的 user 风格字段注入）。

    Args:
        system_prompt: 注册目标，通常传 `ctx.system_prompt`。
        style: 用户风格的 section 文本；传 None 则不注册该段。
    """
    system_prompt.register_section(
        PrompSection(name="identity", order=0, text=Simulator_prompt),
    )
    if style:
        system_prompt.register_section(
            PrompSection(name="user_style", order=1, text=style),
        )

def create_simulator_agent(
    *,
    data_path: Path | None = None,
    session_folder: Path = SESSION_FOLDER,
    name: str = AGENT_NAME,
    session_id: str | None = None,
    step_limit: int = 20,
    max_retry_count: int = 3,
    style: str | None = DEFAULT_USER_STYLE,
) -> AgentContext:
    """创建一个模拟用户 agent。

    Returns:
        装配完成的 AgentContext。驱动执行走 `ctx.agent_loop`，收尾走 `await ctx.close()`。

    注意（TODO）：
    - System Prompt 现由 `register_simulator_system_prompt` 注册（`Simulator_prompt`）；
      后续改为从 `lifeprismTestData/defs/agents/simulator.md` 加载。
    - **暂不注册工具**：本角色只负责对话，不记录数据。

    Args:
        data_path: lifeprism 数据根目录，默认取 lifeprismevalue.config 的配置。
        session_folder: 会话落盘根目录；data_path 会编码为其下的一层项目子目录。
        name: agent 语义名，同时用作会话名。
        session_id: 传入则尝试 load 已有会话，找不到时新建。
        step_limit: 单 turn 最大 step 数（步数兜底）。
        max_retry_count: LLM 调用错误的最大重试次数。
        style: 用户风格的 section 文本；默认 `DEFAULT_USER_STYLE`，传 None 则不注册
            （将来可由用例的 user 风格字段注入，模拟不同用户）。
    """
    data_path = (data_path or get_lifeprism_data_path()).resolve()

    llm_client = OpenAIProvider(
        model=get_llm_model(),
        api_key=get_llm_api_key(),
        base_url=get_llm_base_url(),
    )
    agent_config = AgentConfig(step_limit=step_limit, max_retry_count=max_retry_count)

    # store 只做数据加载：create/load 返回未绑定的 Session，绑定由 AgentContext 构造完成
    store = SessionStore(session_folder)
    session: Session | None = store.load(session_id, data_path) if session_id else None
    if session is None:
        session = store.create(name, data_path)

    ctx = AgentContext(
        agent_name=name,
        session=session,
        agent_config=agent_config,
        llm_client=llm_client,
        prompt_render_parame=None,
    )
    # 提示词注册进 ctx 持有的那一份 SystemPrompt——loop 组装请求用的就是它
    register_simulator_system_prompt(ctx.system_prompt, style=style)

    # 重试策略对象由 ctx 强引用保活（EventService 存的是弱引用）
    llm_retry = LLMRerty()
    ctx.register_policy(AgentPolicySpec(REQUEST_ERROR, [llm_retry.request_error_event], llm_retry))
    return ctx
