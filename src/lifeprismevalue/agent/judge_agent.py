"""裁判 agent（评测的"评估方"）。

角色：在 `judge.mode: model` 时上线，依据用例的 `rubric` 与导出的 `evidence`
判断该用例通过 / 失败并给出理由。

现状：骨架已接好，并把 `Judge_prompt` 注册为 System Prompt（**当前只判"产出是否满足判分要点"**）。
后续：扩展判定内容（如工具调用顺序）；角色提示词改为从
      `lifeprismTestData/defs/agents/judge.md` 加载；本角色只做判定，不注册记录类工具。
"""

from __future__ import annotations

from pathlib import Path

from myagent.agent.core.agent.loop import ReActAgentLoop
from myagent.agent.core.agent.types import AgentConfig
from myagent.agent.core.session import Session, SessionStore
from myagent.agent.core.systemprompt import PrompSection, SystemPrompt
from myagent.agent.llm.llm_retry import LLMRerty
from myagent.agent.llm.openai_provider import OpenAIProvider
from myagent.config.system_config import get_llm_api_key, get_llm_base_url, get_llm_model
from myagent.infra.events import EventService
from myagent.infra.events.eventspec import REQUEST_ERROR

from lifeprismevalue.config import get_lifeprism_data_path

AGENT_NAME = "judge"
# 会话落盘根目录（gitignored）；lifeprism 数据目录编码为其下的一层项目子目录
SESSION_FOLDER = Path("localData")

# 裁判提示词（当前简化版：只判"产出是否满足判分要点"）。
# 输入约定：runner 会把「判分要点 + 本次证据 + 被评估 agent 对话」拼成一条 user 消息发过来，
# 各段的含义与 evidence 的结构见下面的 Evidence_prompt。
Judge_prompt = """
# role
你是一个评测裁判。你的唯一任务是：依据「判分要点」，判断被评估 agent 本次的产出是否达标。

# rule
1. **只以证据为准**，不以被评估 agent 的自我陈述为准——它说“已记录”但证据里没有，判不达标。
2. 判分要点里的每条都要核对；关键字段不符、该记的没记、不该记的多记，都算不达标。
3. 允许被评估 agent 用词不同，但不得偏离判分要点的实质。
4. 只输出 JSON，不要输出任何多余文字。

# 输出格式
{"pass": true, "reason": "一句话说明依据（哪几条满足 / 不满足）"}
"""

# 输入说明：单独一段，专门讲清"发过来的东西是什么"（尤其 evidence.json 的结构）。
# 结构定义与 case.py 的 `_export_evidence`（`evidence.collect_evidence`）保持一致，
# 两者改动需同步——注意归因方式已从「时间窗」改为「与本用例的环境初始态对比」。
Evidence_prompt = """
# 输入说明
你会收到三段内容：

1. **判分要点（rubric）**：本次用例的判定标准，逐条核对它。
2. **本次证据（evidence）**：JSON，来自「本用例跑完后的环境」与「它跑之前的环境初始态」的对比——
   凡是两者不同之处，就是本次运行写下的东西。结构为：
   - `targets`：按用例声明的证据位置；键是声明原值（如 `custom_expense_log`、
     `diary/<year>/<month>/<date>.md`），值是下面两类之一：
     - `kind = "table"`：数据表。
       - `key`：按哪一列对齐；为空表示该表没有主键列，只能整行比对（改动会表现为一删一增）。
       - `rows` / `row_count`：**本次新增的行**（0 = 与初始态一致，即没有新增）。
       - `changed`：同一主键上被改动的行，`changes` 逐列给出 `baseline`（初始态）与 `current`（现在）。
       - `removed`：初始态里有、现在没了的行（本次被删掉的记录）。
       - `columns`：列名；`note` 是判读提示（如"该表无 id 列"）；`error` 非空表示读不到，
         此时该表各项都不可信。
     - `kind = "file"`：文本文件。
       - `changed` 是否被改动，`new_file` 是否本次新建，`deleted` 是否被删掉（初始态有、现在没了）；
       - `diff` 是统一 diff（`--- baseline` / `+++ current`；`+` 开头为新增行，`-` 开头为删除行）；
       - `exists` 为 false 表示该文件现在不存在。
   - `other_changed_files`：**未被用例声明、但确实被改动**的文本文件（结构同 `kind="file"`，
     含被删的），用于发现"顺手改了别的文件"（如改动提示词、日记等）。
   - `other_changed_tables`：**未被用例声明、但确实被改动过**的表（`rows_baseline` → `rows_current`，
     或"行数不变但内容有改动"），用于发现"写到了别的表"。
   - `precondition`：本次运行前写入的预置规则。

3. **对话记录**：被评估 agent 的 user / assistant / tool_result 消息，**仅作参考**；
   判定以证据为准，不以它的自述为准。

# 判读提醒
- 证据是**与初始态的差异**，不是"环境里现在有什么"：初始态就存在的内容不会出现在 `rows` 里。
- 要点说"应新增"→ 看 `rows` / `row_count` / `new_file`；说"应改动 / 应更新"→ 看 `changed` / `diff`；
  说"不得改动 / 不得删除"→ 看 `removed`、`changed`、`other_changed_files` 与 `other_changed_tables`。
- **声明的表是 0 行时，先看 `other_changed_tables`**：可能是"写错地方"（写进了别的表），
  而不是"没做"。两者结论不同，理由里要写清是哪一种，并点名写到了哪张表。
- `error` 非空、或某项 `note` 说不可信时，不要把它当成"agent 没做"，理由里要写明证据不可用。
"""


def build_judge_system_prompt(agent_name: str = AGENT_NAME) -> SystemPrompt:
    """组装裁判 agent 的 System Prompt。

    注册两个 section：
    - `identity`：角色与判定规则（`Judge_prompt`），注册名用 `identity` 以遮蔽全局默认的
      「你是一个个人助手…」，避免与「评测裁判」角色冲突；
    - `evidence_input`：输入说明（`Evidence_prompt`），讲清 rubric / evidence / 对话记录
      三段输入的含义与 evidence 的结构。
    """
    system_prompt = SystemPrompt()
    system_prompt.register_section(
        agent_name,
        PrompSection(name="identity", order=0, text=Judge_prompt),
    )
    system_prompt.register_section(
        agent_name,
        PrompSection(name="evidence_input", order=1, text=Evidence_prompt),
    )
    return system_prompt


def create_judge_agent(
    *,
    data_path: Path | None = None,
    session_folder: Path = SESSION_FOLDER,
    name: str = AGENT_NAME,
    session_id: str | None = None,
    step_limit: int = 20,
    max_retry_count: int = 3,
) -> ReActAgentLoop:
    """创建一个裁判 agent。

    注意（TODO）：
    - System Prompt 现由 `build_judge_system_prompt` 注册（`Judge_prompt`），
      当前只判"产出是否满足判分要点"；后续扩展判定内容（工具调用顺序等）时再补。
    - **暂不注册工具**：本角色只做判定，不记录数据。

    Args:
        data_path: lifeprism 数据根目录，默认取 lifeprismevalue.config 的配置。
        session_folder: 会话落盘根目录；data_path 会编码为其下的一层项目子目录。
        name: agent 名称，同时作为会话名与 SystemPrompt 注册键。
        session_id: 传入则尝试 load 已有会话，找不到时新建。
        step_limit: 单 turn 最大 step 数（步数兜底）。
        max_retry_count: LLM 调用错误的最大重试次数。
    """
    data_path = (data_path or get_lifeprism_data_path()).resolve()

    event_service = EventService()
    llm_retry = LLMRerty()
    event_service.register(REQUEST_ERROR.name, llm_retry.request_error_event)

    store = SessionStore(session_folder, event_service)
    session: Session | None = store.load(session_id, data_path) if session_id else None
    if session is None:
        session = store.create(name, data_path)

    llm_client = OpenAIProvider(
        model=get_llm_model(),
        api_key=get_llm_api_key(),
        base_url=get_llm_base_url(),
    )
    agent_config = AgentConfig(step_limit=step_limit, max_retry_count=max_retry_count)
    agent_loop = ReActAgentLoop(
        event_service,
        session,
        build_judge_system_prompt(name),
        agent_config,
        llm_client,
        name=name,
    )
    # EventService 以弱引用持有订阅者：retry 策略对象必须由外部强引用
    agent_loop.llm_retry = llm_retry
    return agent_loop
