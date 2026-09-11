"""loop 真实链路端到端测试（e2e）。

与单元测试的区别：走真实 LLM API（火山方舟 OpenAI 兼容接口，凭证来自项目根
.env 的 ARK_API_KEY），完整跑通"用户输入 -> LLM 流式回复 -> 工具调用 ->
跨轮上下文 -> session 落盘/load 还原"全链路。

运行方式：
- 只跑 e2e：pytest tests/e2e -v
- 全量但跳过 e2e：pytest -m "not e2e"（e2e 慢且消耗真实 token）
- 缺 ARK_API_KEY 时自动 skip

覆盖三个排查点：
1. session 是否正常保存（文件生成、meta 行、记录 seq 落盘无遗漏）
2. 保存的内容是否正确（类型序列、request/header 快照、工具调用配对、消息内容）
3. 整个流程是否跑通（SessionStore.load 还原后与内存态一致）
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

from myagent.agent.core.agent.loop import ReActAgentLoop
from myagent.agent.core.agent.types import AgentConfig
from myagent.agent.core.provider import ChatParams
from myagent.agent.core.session.store import SessionStore
from myagent.agent.core.systemprompt.systemprompt import SystemPrompt
from myagent.agent.core.systemprompt.types import PrompSection
from myagent.agent.core.tool.tool import Tool
from myagent.agent.llm.openai_provider import OpenAIProvider
from myagent.infra.events.service import EventService
from myagent.utils.helper import project_path_to_session_folder

load_dotenv()
API_KEY = os.getenv("ARK_API_KEY")
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(API_KEY is None, reason="缺少 ARK_API_KEY（项目根 .env），跳过真实 LLM e2e"),
]

AGENT_NAME = "loop_selftest"
PROMPT_SECTION_ORDER = 50  # 标号取 50~99 范围内
MODEL = "doubao-seed-1-6-flash-250828"
BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
SINGLE_TURN_PROMPT = "请帮我查一下2026-09-08北京的天气，顺便告诉我北京市政府的地址"
# 多轮轮次设计：工具轮 -> 换一个工具的轮 -> 纯上下文总结轮
MULTI_TURN_PROMPTS = [
    "请帮我查一下2026-09-08北京的天气",                          # 第1轮：调 get_weather
    "再帮我查一下北京市政府的地址",                               # 第2轮：调 query_address
    "不要调用任何工具，直接用一句话总结我刚才查到的天气和地址信息",  # 第3轮：纯跨轮上下文应答
]


class WeatherTool(Tool):
    """e2e 工具1：查询天气"""

    @property
    def name(self) -> str:
        return "get_weather"

    @property
    def description(self) -> str:
        return "查询指定城市在指定日期的天气状况"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名，如：北京"},
                "date": {"type": "string", "description": "日期，格式YYYY-MM-DD"},
            },
            "required": ["city", "date"],
        }

    async def execute(self, **kwargs) -> str:
        return f"{kwargs.get('city')}在{kwargs.get('date')}的天气是晴，气温18~26℃"


class AddressTool(Tool):
    """e2e 工具2：查询地址"""

    @property
    def name(self) -> str:
        return "query_address"

    @property
    def description(self) -> str:
        return "查询某个地点的详细地址"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "place": {"type": "string", "description": "地点名称，如：北京市政府"},
            },
            "required": ["place"],
        }

    async def execute(self, **kwargs) -> str:
        return f"{kwargs.get('place')}的地址是北京市通州区运河东大街57号"


def check(cond, msg):
    """断言 + 失败信息"""
    assert cond, f"[e2e 失败] {msg}"


def build_agent(session_folder: Path, project_path: Path, session_name: str):
    """组装一套 e2e 组件，返回 (agent_loop, session, store)。

    每次调用都新建 EventService：SessionPresist 按事件名订阅 session/event，
    两个会话共享同一条总线会互相把记录写进对方的持久化 buffer。
    会话文件落在 tmp_path 下，不污染 localData/sessions。
    """
    event_service = EventService()
    store = SessionStore(session_folder, event_service)
    session = store.create(session_name, project_path)

    system_prompt = SystemPrompt()
    system_prompt.register_section(AGENT_NAME, PrompSection(
        name="tool_guide",
        order=PROMPT_SECTION_ORDER,
        text="你可以调用工具查询天气与地址。拿到工具结果后，用中文简洁地汇总回答用户。",
    ))

    llm_client = OpenAIProvider(
        model=MODEL,
        api_key=API_KEY,
        base_url=BASE_URL,
        chat_params=ChatParams(temperature=0.7),  # request/header 的 params 快照取自 provider
    )
    agent_config = AgentConfig(
        step_limit=20,  # 步数兜底（单轮 2 工具 + 汇总回复约 3 步）；采样参数由 provider 的 chat_params 提供
    )
    agent_loop = ReActAgentLoop(
        event_service, session, system_prompt,
        agent_config, llm_client, name=AGENT_NAME,
        prompt_render_parame={},  # 渲染参数是 loop 构造参数，不归 AgentConfig
    )
    agent_loop.tool_register.register([WeatherTool(), AddressTool()])
    return agent_loop, session, store


def session_file_of(store: SessionStore, session, project_path: Path) -> Path:
    """按 store 的编码规则定位会话文件（与 SessionStore._session_file 一致）"""
    return project_path_to_session_folder(project_path, store.session_folder) / f"{session.meta_data.session_id}.jsonl"


def read_disk_records(session_file: Path) -> list[dict]:
    """读取会话文件：第1行 meta + 其余记录行，返回记录 dict 列表"""
    lines = session_file.read_text(encoding="utf-8").splitlines()
    meta_line = json.loads(lines[0])
    return meta_line, [json.loads(line) for line in lines[1:]]


@pytest.mark.asyncio
async def test_单轮_真实LLM完整链路_保存与还原(tmp_path):
    """一轮完整 turn：用户提问 -> 模型调天气/地址两个工具 -> 汇总回复，session 全程落盘并可还原。

    排查点：
    1. session 正常保存：文件生成、meta 行正确、记录 seq 落盘无遗漏
    2. 内容正确：类型序列、request/header 快照、工具调用配对、消息内容
    3. 流程跑通：load 还原后与内存态一致
    """
    project_path = tmp_path / "proj"
    agent_loop, session, store = build_agent(tmp_path / "sessions", project_path, "e2e单轮链路")

    await agent_loop.send(SINGLE_TURN_PROMPT)
    # 收尾：强制把 buffer 里剩余记录（工具结果、step/end、turn/end 等）落盘
    session.presistence.presist()

    # ---- 排查点 1：session 是否正常保存 ----
    session_file = session_file_of(store, session, project_path)
    check(session_file.exists(), f"会话文件已生成: {session_file}")
    meta_line, disk_records = read_disk_records(session_file)
    check(meta_line.get("type") == "meta_data", "第1行是 meta_data")
    check(meta_line.get("session_id") == session.meta_data.session_id, "meta 的 session_id 与内存一致")
    # 每条内存记录都要落盘：直接记录按 seq 命中，assistant/chunk 体现在 text-chunk 的 source_event_seqs
    disk_seqs = set()
    for r in disk_records:
        if r["type"] == "text-chunk":
            disk_seqs.update(r.get("source_event_seqs") or [])
        else:
            disk_seqs.add(r["seq"])
    memory_seqs = {r.seq for r in session.record_list}
    check(disk_seqs == memory_seqs, f"落盘记录 seq 覆盖完整（共 {len(memory_seqs)} 条无遗漏）")

    # ---- 排查点 2：保存的内容是否正确 ----
    line_seqs = [r["seq"] for r in disk_records]
    check(line_seqs == sorted(line_seqs) and len(set(line_seqs)) == len(line_seqs), "磁盘记录按 seq 单调不重复")
    check(disk_records[0]["type"] == "turn/start", "首条记录为 turn/start")
    check(disk_records[-1]["type"] == "turn/end", "末条记录为 turn/end")
    by_type: dict[str, list] = {}
    for r in disk_records:
        by_type.setdefault(r["type"], []).append(r)
    check(all(r["turn"] == 1 for r in disk_records), "全部记录 turn=1")
    steps = [r["step"] for r in by_type.get("step/start", [])]
    check(len(steps) >= 2 and steps == sorted(steps), f"至少 2 步且 step 编号单调递增: {steps}")
    check(len(by_type.get("request/header", [])) == 1, "request/header 仅首写 1 条（配置未变不重复写）")
    header = by_type["request/header"][0]["data"]
    check(header["reason"] == "initial" and header["model_name"] == MODEL, "request/header 记录 reason/model_name")
    schema_names = {t["function"]["name"] for t in header["tools"]}
    check(schema_names == {"get_weather", "query_address"}, f"request/header 记录 2 个工具 schema: {sorted(schema_names)}")
    check("查询天气与地址" in header["system_prompt"], "system_prompt 含注册段的文本")
    check(header["params"]["temperature"] == 0.7, "request/header 记录采样参数 temperature=0.7")
    user_msgs = by_type.get("user/message", [])
    check(len(user_msgs) == 1, "user/message 只写 1 条（工具循环不重复写用户消息）")
    check(user_msgs[0]["data"]["message"]["content"] == [{"type": "text", "text": SINGLE_TURN_PROMPT}], "user/message 内容与输入一致")
    calls = {r["data"]["call_id"]: r["data"] for r in by_type.get("tool/call", [])}
    results = by_type.get("tool/result", [])
    check(len(results) == len(calls) and len(calls) >= 1, f"tool/call({len(calls)}) 与 tool/result({len(results)}) 一一配对")
    called_names = set()
    for r in results:
        d = r["data"]
        check(d["call_id"] in calls, f"tool/result(call_id={d['call_id'][:12]}...) 有配对的 tool/call")
        check(d["message"]["role"] == "tool" and d["message"]["tool_call_id"] == d["call_id"], "tool/result 消息 role/tool_call_id 正确")
        called_names.add(d["tool_name"])
    check(called_names == {"get_weather", "query_address"}, f"两个工具都被真实调用: {sorted(called_names)}")
    final = by_type.get("assistant/message", [])[-1]
    check(bool(final["data"]["message"]["content"]), "最后一条 assistant/message 有非空回复内容")

    # ---- 排查点 3：load 还原后与内存态一致 ----
    restored = store.load(session.meta_data.session_id, project_path)
    check(restored is not None, "load 成功还原会话")
    check([r.type for r in restored.record_list] == [r.type for r in session.record_list], "还原后记录类型序列一致")
    check([r.seq for r in restored.record_list] == [r.seq for r in session.record_list], "还原后 seq 序列一致")
    check(restored.presistence is not None, "还原的会话带持久化组件（可继续对话）")
    mem_msgs, restored_msgs = session.derive_messages(), restored.derive_messages()
    check([m.role for m in restored_msgs] == [m.role for m in mem_msgs], f"还原后消息面一致: {[m.role for m in mem_msgs]}")
    check(restored_msgs == mem_msgs, "还原后消息逐条内容一致（user/assistant/tool 全对齐）")
    # 最终回答引用了工具结果（模型真的看到了工具返回）
    check("晴" in mem_msgs[-1].content and "运河东大街" in mem_msgs[-1].content, "最终回复引用了两个工具的结果")


@pytest.mark.asyncio
async def test_多轮_跨轮上下文与session累积(tmp_path):
    """3 轮对话：天气工具 -> 地址工具 -> 不调工具的跨轮总结。

    相比单轮新增的排查点：
    - 跨轮累积：turn/start、turn/end、user/message 各 3 条，step 每轮从 1 重新计数
    - request/header 跨轮去重：配置不变时 3 轮只写 1 条
    - 上下文连续性：每轮消息面只追加不篡改；第 3 轮回答必须引用前两轮工具结果
      （"运河东大街57号"只存在于工具返回里，模型不可能凭空猜出）
    - load 还原后三轮消息面一致
    """
    project_path = tmp_path / "proj"
    agent_loop, session, store = build_agent(tmp_path / "sessions", project_path, "e2e多轮链路")

    # ---- 逐轮对话：每轮前后对比消息面，验证历史只增不改 ----
    for i, prompt in enumerate(MULTI_TURN_PROMPTS, 1):
        before = session.derive_messages()
        await agent_loop.send(prompt)
        # 每轮结束强制把剩余记录（工具结果、step/end、turn/end 等）落盘
        session.presistence.presist()
        after = session.derive_messages()
        check(after[:len(before)] == before, f"第{i}轮后旧历史完整保留（消息面只追加不篡改）")
        check(len(after) > len(before) and after[len(before)].role == "user", f"第{i}轮以新 user 消息追加进消息面")

    # ---- 排查点 1：session 是否正常保存 ----
    session_file = session_file_of(store, session, project_path)
    check(session_file.exists(), f"会话文件已生成: {session_file}")
    meta_line, disk_records = read_disk_records(session_file)
    check(meta_line.get("type") == "meta_data" and meta_line.get("session_id") == session.meta_data.session_id,
          "第1行是 meta_data 且 session_id 与内存一致")
    disk_seqs = set()
    for r in disk_records:
        if r["type"] == "text-chunk":
            disk_seqs.update(r.get("source_event_seqs") or [])
        else:
            disk_seqs.add(r["seq"])
    memory_seqs = {r.seq for r in session.record_list}
    check(disk_seqs == memory_seqs, f"三轮落盘记录 seq 覆盖完整（共 {len(memory_seqs)} 条无遗漏）")

    # ---- 排查点 2：保存的内容是否正确（多轮特有） ----
    by_type: dict[str, list] = {}
    for r in disk_records:
        by_type.setdefault(r["type"], []).append(r)
    check(disk_records[0]["type"] == "turn/start" and disk_records[-1]["type"] == "turn/end", "首条 turn/start，末条 turn/end")
    check([r["turn"] for r in by_type["turn/start"]] == [1, 2, 3], "turn/start 恰好 3 条且 turn 编号 1/2/3")
    check(len(by_type["turn/end"]) == 3, "turn/end 恰好 3 条（每轮一条）")
    user_texts = [u["data"]["message"]["content"][0]["text"] for u in by_type.get("user/message", [])]
    check(user_texts == MULTI_TURN_PROMPTS, "三轮 user/message 顺序与输入一致")
    check(len(by_type.get("request/header", [])) == 1, "request/header 三轮仅首写 1 条（配置未变不跨轮重复写）")
    for t in (1, 2, 3):
        steps_in_turn = [r["step"] for r in by_type["step/start"] if r["turn"] == t]
        check(steps_in_turn[0] == 1 and steps_in_turn == sorted(steps_in_turn), f"第{t}轮 step 从 1 重新计数且单调: {steps_in_turn}")
    check(len([r for r in by_type["step/start"] if r["turn"] == 3]) == 1, "第3轮（无工具调用）只有 1 个 step")
    call_turns: dict[int, set] = {}
    for r in by_type.get("tool/call", []):
        call_turns.setdefault(r["turn"], set()).add(r["data"]["tool_name"])
    check(call_turns.get(1) == {"get_weather"}, "第1轮只调用天气工具")
    check(call_turns.get(2) == {"query_address"}, "第2轮只调用地址工具")
    check(3 not in call_turns, "第3轮按要求未调用工具（纯上下文应答）")
    calls = {r["data"]["call_id"]: r["data"] for r in by_type.get("tool/call", [])}
    for r in by_type.get("tool/result", []):
        d = r["data"]
        check(d["call_id"] in calls and d["message"]["tool_call_id"] == d["call_id"],
              f"tool/result(call_id={d['call_id'][:12]}...) 与 tool/call 配对")
    check(all(r["turn"] in (1, 2, 3) for r in disk_records), "全部记录 turn 都落在 1~3")
    # 跨轮上下文的直接证据：第3轮回答引用只有工具结果里才有的信息
    final_answer = session.derive_messages()[-1].content
    check("晴" in final_answer and "运河东大街" in final_answer, "第3轮总结引用了前两轮工具结果（跨轮上下文生效）")

    # ---- 排查点 3：load 还原后与内存态一致 ----
    restored = store.load(session.meta_data.session_id, project_path)
    check(restored is not None, "load 成功还原会话")
    check([r.type for r in restored.record_list] == [r.type for r in session.record_list], "还原后记录类型序列一致")
    check([r.seq for r in restored.record_list] == [r.seq for r in session.record_list], "还原后 seq 序列一致")
    mem_msgs, restored_msgs = session.derive_messages(), restored.derive_messages()
    check(restored_msgs == mem_msgs, "还原后三轮消息面逐条内容一致")
