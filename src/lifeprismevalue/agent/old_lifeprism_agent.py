"""临时脚本：复刻 v1 版 lifeprism 的 chat 模式 agent，用于和 myagent 版本对比。

对应旧实现 lifeprism/llm/agent/loop.py 的 CHAT 分支
（_process_msg -> Context.build_system_prompt），提示词拆成两部分：

1. System Prompt（按 order 升序拼接，order 从 0 开始）
   identity -> soul -> agent -> tool                        ← 来自提示词版本库
   -> skill-list -> user/user.md -> recent_state.md          ← 运行时扫描 / 用户数据
2. System Reminder（Message List 第二位，System Prompt 之后）
   custom_prompt.md

前四段（identity / soul / agent / tool）从 `lifeprismData/prompts/agent_prompts.yaml`
按版本取（见 lifeprismevalue.prompts）：**切换提示词版本就是改该文件的
active_version**，不需要动代码。后三段（skill-list / user / recent_state）是运行时
扫描与用户数据，不进版本库。其中 agent 段含 {agent_path} 等参数，注册为 callback
section，由 SystemPrompt.render 在组装请求时通过 prompt_render_parame 注入。

create_old_agent() 返回组装好的 ReActAgentLoop（提示词、工具、会话、重试策略均已接线）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from myagent.agent.core.agent.loop import ReActAgentLoop
from myagent.agent.core.agent.types import AgentConfig
from myagent.agent.core.session import Session, SessionStore
from myagent.agent.core.systemprompt import PrompSection, SystemPrompt
from myagent.agent.llm.llm_retry import LLMRerty
from myagent.agent.llm.openai_provider import OpenAIProvider
from myagent.infra.events import EventService
from myagent.infra.events.eventspec import REQUEST_ERROR

from lifeprismevalue.config import get_lifeprism_data_path
from lifeprismevalue.prompts import PromptLoader
from lifeprismevalue.tools import build_lifeprism_tools

load_dotenv()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

AGENT_NAME = "old_agent"
# 会话落盘根目录（gitignored），lifeprism 数据目录编码为其中的一层项目子目录
SESSION_FOLDER = Path("localData")
# 文件工具白名单，与旧 lifeprism 的 ALLOWED_DIRS 对齐（identity 末尾的目录说明会用到）
ALLOWED_DIRS = ["user", "diary", "agent"]

MODEL = os.getenv("LIFEPRISM_MODEL", "doubao-seed-1-6-flash-250828")
BASE_URL = os.getenv("LIFEPRISM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")


class _SafeDict(dict):
    """format_map 用：缺失占位符原样保留，不抛 KeyError。"""

    def __missing__(self, key):
        return "{" + key + "}"


def _placeholder_section(content: str):
    """把含 {占位符} 的段包成 callback section：渲染时注入参数，缺参数则原样保留。

    用 _SafeDict 而不是 str.format：正文里可能含并非占位符的花括号（如
    `{skill同名文件.md}`），缺参数时原样保留，不会抛 KeyError。
    """

    def render(**params) -> str:
        return content.format_map(_SafeDict(params))

    return render


def _read_prompt_file(path: Path) -> str | None:
    """读取提示词文件；不存在返回 None（对应旧实现“未加载该段”）。"""
    if not path.exists():
        logger.warning("提示词文件不存在，跳过: %s", path)
        return None
    return path.read_text(encoding="utf-8")


def _build_expand_dir(data_path: Path) -> str:
    """读取额外工作目录列表（对齐旧 Context._build_expand_dir，缺失返回“无”）。"""
    meta_path = data_path / "localData" / "expand_dir" / "expand_meta_data.json"
    if not meta_path.exists():
        return "无"
    try:
        expand_list = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("读取 expand_meta_data.json 失败: %s", e)
        return "无"
    if not expand_list:
        return "无"
    return "\n".join(
        f"- {item.get('path', '')} ({item.get('path_name', '')}): {item.get('description', '')}"
        for item in expand_list
    )


def _agent_md_params(data_path: Path) -> dict[str, str]:
    """agent.md 的占位符注入参数（对齐旧 Context._build_bootstrap）。"""
    return {
        "agent_path": str(data_path / "agent"),
        "user_path": str(data_path / "user"),
        "diary_path": str(data_path / "diary"),
        "expand_dir": _build_expand_dir(data_path),
    }


def _read_skill_meta(skill_md: Path) -> dict[str, str]:
    """解析 SKILL.md 顶部 YAML frontmatter 的 name/description。

    只做简单的逐行 key: value 解析（不引入 yaml 依赖）；无 frontmatter 返回空字典。
    """
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        meta[key.strip()] = value.strip().strip("\"'")
    return meta


def _build_skill_list(data_path: Path) -> str:
    """扫描 agent/skills 下的技能，拼成 skill-list 段。

    每个技能以 `## 名称` 开头，后接 SKILL.md frontmatter 中的 description；
    没有 skills 目录或未发现任何技能时返回空串（该段不注册）。
    """
    skills_dir = data_path / "agent" / "skills"
    if not skills_dir.is_dir():
        return ""
    entries: list[str] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        meta = _read_skill_meta(skill_md)
        name = meta.get("name") or skill_md.parent.name
        description = meta.get("description", "")
        entries.append(f"## {name}\n{description}".rstrip())
    if not entries:
        return ""
    return "# skill-list\n\n" + "\n\n".join(entries)


def build_chat_system_prompt(
    data_path: Path | None = None, prompt_version: str | None = None
) -> SystemPrompt:
    """复刻旧 chat 模式的提示词组装，返回注册完成的 SystemPrompt。

    分段顺序与旧 chat 模式一致（order 从 0 开始），来源分两类：

    - **版本库段**：identity / soul / agent / tool 从
      `<data_path>/prompts/agent_prompts.yaml` 按版本取。`prompt_version` 为空时
      取该文件的 `active_version`——**切换提示词版本即改它**。
    - **运行时段**：skill_list（扫描 agent/skills）与 user / recent_state（用户数据）
      仍按原方式加载，不进版本库。

    - agent 段：含占位符，注册为 callback section，参数由 SystemPrompt.render 注入
    - custom_prompt.md：注册为 System Reminder，不进 System Prompt（评测时是变量）

    注意：渲染 agent 段需要给 ReActAgentLoop 传 prompt_render_parame
    （见 create_old_agent），否则 render 会因缺少参数而跳过该段。

    Args:
        data_path: lifeprism 数据根目录，默认取 lifeprismevalue.config 的配置。
        prompt_version: 提示词版本名；为空取版本库的 active_version。

    Raises:
        FileNotFoundError: 版本库文件不存在。
        ValueError: 版本名不存在，或正文不符合「段从一级标题开始」等格式约定。
    """
    data_path = (data_path or get_lifeprism_data_path()).resolve()
    chat_dir = data_path / "agent" / "chat"

    system_prompt = SystemPrompt()

    # ---- 版本库段：(section 名, order, 版本库里的段名) ----
    loader = PromptLoader.for_data_path(data_path)
    version = prompt_version or loader.active_version
    versioned_sections: list[tuple[str, int, str]] = [
        ("identity", 0, "identity"),
        ("soul", 1, "soul"),
        ("agent", 2, "agent"),
        ("tool", 3, "tool"),
    ]
    segments = loader.load_segments(version)
    for section_name, order, segment_name in versioned_sections:
        content = segments.get(segment_name)
        if content is None:
            logger.warning("提示词版本 %s 缺少段 %s，跳过该段", version, segment_name)
            continue
        if section_name == "identity":
            # 旧实现会在 identity 末尾追加工作目录与可操作目录说明
            content += (
                f"\n你当前工作目录是：{data_path}，"
                f"你能够阅读和操作的目录是：{data_path}/{ALLOWED_DIRS}"
            )
        # agent 段含 {agent_path}/{user_path}/{diary_path}/{expand_dir} 占位符：
        # 注册为 callback，参数由 SystemPrompt.render 注入（见 create_old_agent）
        text = _placeholder_section(content) if section_name == "agent" else content
        system_prompt.register_section(
            AGENT_NAME, PrompSection(name=section_name, order=order, text=text)
        )

    # ---- 运行时段 ----
    # skill-list：列出 agent/skills 下各 skill 的名称与描述，紧随 tool 之后
    skill_list = _build_skill_list(data_path)
    if skill_list:
        system_prompt.register_section(
            AGENT_NAME, PrompSection(name="skill_list", order=4, text=skill_list)
        )

    # user / recent_state 是用户数据，按原方式从文件读取
    runtime_sections: list[tuple[str, int, Path]] = [
        ("user", 5, data_path / "user" / "user.md"),
        ("recent_state", 6, data_path / "user" / "daily_data" / "recent_state.md"),
    ]
    for name, order, path in runtime_sections:
        content = _read_prompt_file(path)
        if content is None:
            continue
        system_prompt.register_section(
            AGENT_NAME, PrompSection(name=name, order=order, text=content)
        )

    # System Reminder：作为 Message List 第二位注入（System Prompt 之后、正常对话之前）
    custom_prompt = _read_prompt_file(chat_dir / "custom_prompt.md")
    if custom_prompt is not None:
        system_prompt.register_system_reminder(AGENT_NAME, custom_prompt)

    return system_prompt


def create_old_agent(
    *,
    data_path: Path | None = None,
    session_folder: Path = SESSION_FOLDER,
    name: str = AGENT_NAME,
    session_id: str | None = None,
    step_limit: int = 20,
    max_retry_count: int = 3,
) -> ReActAgentLoop:
    """创建一个复刻旧 lifeprism chat 模式配置的 agent loop。

    Args:
        data_path: lifeprism 数据根目录，默认取 lifeprismevalue.config 的配置。
        session_folder: 会话落盘根目录；data_path 会编码为其下的一层项目子目录。
        name: agent 名称，同时作为 SystemPrompt 的注册名（隔离/遮蔽的键）。
        session_id: 传入则尝试 load 已有会话，找不到时新建。
        step_limit: 单 turn 最大 step 数（步数兜底）。
        max_retry_count: LLM 调用错误的最大重试次数。
    """
    data_path = (data_path or get_lifeprism_data_path()).resolve()

    event_service = EventService()
    # 重试策略：订阅 request/error（waterfall 语义），沿用 lifeprism 的错误分类与退避
    llm_retry = LLMRerty()
    event_service.register(REQUEST_ERROR.name, llm_retry.request_error_event)

    store = SessionStore(session_folder, event_service)
    session: Session | None = store.load(session_id, data_path) if session_id else None
    if session is None:
        session = store.create(name, data_path)

    llm_client = OpenAIProvider(
        model=MODEL,
        api_key=os.getenv("ARK_API_KEY", ""),
        base_url=BASE_URL,
    )
    agent_config = AgentConfig(step_limit=step_limit, max_retry_count=max_retry_count)
    agent_loop = ReActAgentLoop(
        event_service,
        session,
        build_chat_system_prompt(data_path),
        agent_config,
        llm_client,
        name=name,
        # agent.md 的占位符参数，交给 SystemPrompt.render 在组装请求时注入
        prompt_render_parame={"agent": _agent_md_params(data_path)},
    )
    # EventService 以弱引用持有订阅者：retry 策略对象必须由外部强引用，否则会被回收
    agent_loop.llm_retry = llm_retry

    agent_loop.tool_register.register(build_lifeprism_tools())
    return agent_loop
