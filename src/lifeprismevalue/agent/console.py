"""简易控制台：在终端里直接和 lifeprism agent 对话，并把执行过程打印到控制台。

用途：本地手工测试 agent 的对话与工具调用。不开额外窗口/UI，所有内容直接 print。

模块组成：
- ConsoleMonitor：订阅 session/event，按记录类型分流打印四类关键事实
    * assistant/message ：推理内容(reasoning_content) + 正文 + 工具调用请求
    * tool/call         ：工具名 + 模型发起的原始 arguments(JSON 字符串)
    * tool/result       ：工具执行结果（含失败标记与耗时，正文超长则截断）
    * turn/end          ：一轮结束（输出结束，可以继续对话）
  说明：assistant/message、tool/call、turn/end 三个「运行时事件」的 payload 目前
  是占位空壳（见 myagent/infra/events/payload.py），完整数据只落在 session 记录
  里；而 session 每次 append 都会触发 session/event，且 payload 携带刚落盘的记录。
  所以这里统一从 session/event 分流：既能监控这四类事实，也拿得到完整内容。
  （tool/result 的运行时 payload 其实已填全，但为与其余几类同走一条路，仍从
  session/event 取。）
- ConsoleChannel：把控制台本身当人在回路的应答通道（HITLChannel 实现）
- main：读取控制台输入 -> agent.followup 入队；/cancel 调 agent.cancel() 打断当前输出。

护栏（ToolUseGuard）只在这里挂：它是本地手工验证用的组件，不进 create_old_agent，
免得评测/别的入口被动带上。白名单见 ALLOW_PATH，改这里就能试"拒"与"放行"两种情况。

人在回路（HITL）同样只在这里挂：request/error 上接两个订阅方——step 超限与工具熔断
——它们都由控制台向人提问（见 ConsoleChannel）。step_limit 调成 1 便于一步触顶。

控制台输入：
    普通文本  作为用户消息发送（agent 正在输出时输入会先入队，本轮结束后执行）
    /cancel   取消当前在途 turn
    /exit     退出控制台
    选项编号  当 [人在回路] 的提问挂在控制台上时，这一行被当作应答（见 ConsoleChannel）

运行：
    python -m lifeprismevalue.agent.console
"""

from __future__ import annotations

import asyncio
import re

from myagent.agent.core.session.types import (
    AssistantMessageData,
    ToolCallData,
    ToolResultData,
    TurnEndData,
)
from myagent.agent.guard.tool_use_guard import ToolUseGuard
from myagent.agent.hitl.hitl import HITL
from myagent.agent.hitl.types import HITLMessage, HumanReturn
from myagent.infra.events.eventspec import REQUEST_ERROR, SESSION_EVENT, TOOL_CALL
from myagent.infra.events.payload import SessionEventPayload

from lifeprismevalue.agent.old_lifeprism_agent import create_old_agent

# 各模块输出之间的分割线：控制台里把不同事件的打印块隔开，便于查看
_SEP = "-" * 64

# 两条输入提示：正常对话与"有人在等应答"两种状态用不同提示，避免误把选项当消息
_PROMPT_USER = "\n你> "
_PROMPT_CHOICE = "\n选择> "

# 工具结果正文的打印上限：读文件类工具一次可回整个文件，控制台不做限制的话
# 会把整轮对话淹掉。超长截断并标注丢了多少字符，避免看着像完整结果。
_TOOL_RESULT_MAX_CHARS = 500

# 路径护栏白名单（绝对路径，前缀匹配到目录则其下全部放行）。
#   ✅ 放行 lifeprism 数据根下的一切：想验证"工具照常执行"就留这一条
#   ⛔ 清空成 []：fail-closed，任何文件工具调用都会被拒——用来验证"护栏拦得住"
# ALLOW_PATH: list[str] = [r"D:\desktop\软件开发\agent\lifeprismData"]
ALLOW_PATH =[]
# 人在回路的装配参数（手工验证时改这里即可）
HITL_STEP_LIMIT = 20     # 单 turn 步数上限：调成 1 使首次工具调用后即触顶
HITL_GRANT_STEPS = 5    # 选"继续"时放宽的步数
HITL_TIMEOUT = 300.0    # 等人类应答的秒数；手工操作比默认 60s 宽松


class ConsoleMonitor:
    """把 agent 的关键执行事件打印到控制台（只读订阅，不干预循环）。"""

    def __init__(self, event_service) -> None:
        self._event_service = event_service
        # EventService 以弱引用(WeakMethod)持有回调：实例必须由调用方强引用，
        # 否则订阅在实例被回收后立即失效
        self._event_service.register(SESSION_EVENT.name, self._on_session_event)

    def _on_session_event(self, payload: SessionEventPayload) -> None:
        """session/event 回调：按记录类型分流到对应打印。"""
        record = payload.session_record
        if record.type == "assistant/message":
            self._print_assistant_message(record)
        elif record.type == "tool/call":
            self._print_tool_call(record)
        elif record.type == "tool/result":
            self._print_tool_result(record)
        elif record.type == "turn/end":
            self._print_turn_end(record)

    # ---------- 各类记录的打印 ----------

    def _print_assistant_message(self, record) -> None:
        """assistant/message：打印推理过程、正文与模型发起的工具调用请求。"""
        data: AssistantMessageData = record.data
        message = data.message
        print(f"\n{_SEP}")
        print(f"[assistant/message] turn={record.turn} step={record.step}")
        if message.reasoning_content:
            print(f"  推理内容: {message.reasoning_content}")
        if message.content:
            print(f"-"*60)
            print(f"  回复内容: {message.content}")
        for call in message.tool_calls or []:
            print(f"  工具调用请求: {call.name} arguments={call.arguments}")

    def _print_tool_call(self, record) -> None:
        """tool/call：打印工具名与模型发起的原始参数。"""
        data: ToolCallData = record.data
        print(_SEP)
        print(
            f"[tool/call] turn={record.turn} step={record.step} "
            f"{data.tool_name} arguments={data.arguments}"
        )

    def _print_tool_result(self, record) -> None:
        """tool/result：打印工具执行结果——失败标记、耗时与回喂给模型的内容。"""
        data: ToolResultData = record.data
        print(_SEP)
        print(
            f"[tool/result] turn={record.turn} step={record.step} "
            f"{data.tool_name} is_error={data.is_error} duration_ms={data.duration_ms}"
        )
        print(f"  返回内容: {self._clip(data.message.content)}")

    @staticmethod
    def _clip(content: str | list) -> str:
        """把结果正文压成可打印文本：分段列表拼出文本段，超长截断并标注丢弃量。

        Message.content 声明为 str | list（多模态分段），工具结果实际是 str，
        但这里按声明兜住两种形态，免得打印时抛异常把整条监控打断。
        """
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
                elif isinstance(part, str):
                    parts.append(part)
            content = "".join(parts)
        if content is None:
            return ""
        text = content if isinstance(content, str) else str(content)
        if len(text) <= _TOOL_RESULT_MAX_CHARS:
            return text
        dropped = len(text) - _TOOL_RESULT_MAX_CHARS
        return text[:_TOOL_RESULT_MAX_CHARS] + f"…[截断 {dropped} 字符]"

    def _print_turn_end(self, record) -> None:
        """turn/end：一轮结束，控制台可继续输入下一句。"""
        data: TurnEndData = record.data
        print(_SEP)
        print(
            f"[turn/end] turn={record.turn} "
            f"结束原因={data.reason_type} {data.reason_text}"
        )
        print(f"{_SEP}\n=== 输出结束，可以继续输入 ===")


class ConsoleChannel:
    """把控制台当人机通道：ask_human 把问题挂出来，等主循环把答案喂回来。

    **单一读点原则**：stdin 只能有一个读取者在等——即主循环的 _aio_input。本通道
    若自己再开一个 input()，两个阻塞读会抢同一行，谁拿到不确定（这正是"人在回路
    的提问被消息输入抢走"的原因）。故这里不读输入，只把待答请求挂出来（waiting），
    由主循环读到行后调用 answer() 路由过来。

    两者都在事件循环里跑，waiting 的读写不跨线程，无需加锁。
    """

    def __init__(self) -> None:
        self._pending: HITLMessage | None = None
        self._answer: asyncio.Future | None = None

    @property
    def waiting(self) -> bool:
        """是否有提问正等人类应答（主循环据此决定把读到的行喂给谁）。"""
        return self._pending is not None

    async def ask_human(self, message: HITLMessage) -> HumanReturn:
        """HITLChannel 协议实现：打印问题与选项，挂起等主循环把答案喂回。"""
        self._pending = message
        self._answer = asyncio.get_running_loop().create_future()
        self._print_question(message)
        try:
            return await self._answer
        finally:
            # 兜底清理：正常路径已由 answer() 摘除，这里覆盖取消/异常提前退出的情况
            self._pending = None
            self._answer = None

    def answer(self, line: str) -> bool:
        """主循环把读到的一行喂进来；无法识别为选项时返回 False（不改任何状态）。

        识别成功时**立即**摘除 pending：否则主循环下一轮的提示会仍显示"选择>"，
        而实际这一问已经答完了。
        """
        if self._answer is None or self._pending is None:
            return False
        choice_id = self._parse(line, self._pending)
        if choice_id is None:
            return False
        answer, self._answer = self._answer, None
        self._pending = None
        answer.set_result(HumanReturn(choice_id=choice_id, content=line))
        return True

    @staticmethod
    def _parse(line: str, message: HITLMessage) -> str | list[str] | None:
        """把一行输入解析成 choice_id：接受选项编号（从 1 起，与打印一致）或 choice_id 原文。

        多选按逗号/空格切分成多个；单选只取第一个。任一项无法识别（编号越界、
        id 不存在）即整体返回 None，由调用方提示重输——不做"部分接受"。
        """
        tokens = [t for t in re.split(r"[,\s]+", line.strip()) if t]
        if not tokens:
            return None
        ids: list[str] = []
        for token in tokens:
            matched = ConsoleChannel._to_choice_id(token, message)
            if matched is None:
                return None
            ids.append(matched)
        if message.human_return_type == "multiple-select":
            return ids
        return ids[0]

    @staticmethod
    def _to_choice_id(token: str, message: HITLMessage) -> str | None:
        """单个 token -> choice_id：数字按 1 起编号取，否则按 id 精确匹配。"""
        if token.isdigit():
            index = int(token)
            if 1 <= index <= len(message.choices):
                return message.choices[index - 1].choice_id
            return None
        for choice in message.choices:
            if choice.choice_id == token:
                return token
        return None

    @staticmethod
    def _print_question(message: HITLMessage) -> None:
        """打印提问与编号选项（编号即 answer 接受的输入形式）。"""
        print(f"\n{_SEP}")
        print(f"[人在回路] {message.content}")
        for index, choice in enumerate(message.choices, start=1):
            desc = f" —— {choice.description}" if choice.description else ""
            print(f"  [{index}] {choice.choice_name}{desc}")
        print(_SEP)


async def _aio_input(prompt: str) -> str:
    """在 executor 里执行阻塞的 input()，避免卡住事件循环（agent 后台继续跑）。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


async def _main() -> None:
    agent = create_old_agent(step_limit=HITL_STEP_LIMIT)
    # event_service 由 create_old_agent 内部创建并被 loop 持有；这里取来挂监控。
    # monitor 必须保持强引用（见 ConsoleMonitor.__init__ 的弱引用说明），
    # 它是 _main 的局部变量，函数存活期间不会被回收。
    monitor = ConsoleMonitor(agent._event_service)

    # 挂路径护栏：订阅 tool/call（waterfall 语义），在工具执行前逐条裁决文件路径。
    # 同 monitor，它也必须由 _main 的局部变量强引用——EventService 存的是 WeakMethod，
    # 没人强引用的话注册当场失效（不报错，只是护栏永远不生效）。
    tool_use_guard = ToolUseGuard({"allow_path": ALLOW_PATH})
    agent._event_service.register(TOOL_CALL.name, tool_use_guard.file_sys_path_guard)

    # 挂人在回路：request/error 上按序接两个订阅方。两者管辖的错误类型不重叠
    # （step 超限 / 工具熔断），前面那个不认领时会 await _next() 交给下一个；
    # create_old_agent 已注册的 LLMRerty 在最外层，对这两类错误同样不认领。
    # 强引用要求同 monitor：hitl 由局部变量持有，channel 由 hitl 持有。
    channel = ConsoleChannel()
    hitl = HITL(channel, grant_steps=HITL_GRANT_STEPS, timeout=HITL_TIMEOUT)
    agent._event_service.register(REQUEST_ERROR.name, hitl.maxstep_continue)
    agent._event_service.register(REQUEST_ERROR.name, hitl.tool_breaker_continue)

    agent.start()
    print("=== lifeprism agent 控制台 ===")
    print("直接输入消息对话；/cancel 取消当前输出；/exit 退出")
    try:
        while True:
            try:
                line = (await _aio_input(_PROMPT_CHOICE if channel.waiting else _PROMPT_USER)).strip()
            except (EOFError, KeyboardInterrupt):
                break
            # 有人在等应答：这一行归 HITL，不当消息。注意先于空行判断——空行对选项
            # 解析无意义，但仍要走 answer 让它提示重输，而不是静默吞掉
            if channel.waiting:
                if not channel.answer(line):
                    print("[console] 无法识别的选项，请重新输入")
                continue
            if not line:
                continue
            if line == "/exit":
                break
            if line == "/cancel":
                agent.cancel()
                print("[console] 已取消当前输出，可继续输入")
                continue
            agent.followup(line)
    finally:
        agent.cancel()
        print("已退出控制台")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
