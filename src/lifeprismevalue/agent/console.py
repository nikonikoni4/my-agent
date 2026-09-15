"""简易控制台：在终端里直接和 lifeprism agent 对话，并把执行过程打印到控制台。

用途：本地手工测试 agent 的对话与工具调用。不开额外窗口/UI，所有内容直接 print。

模块组成：
- ConsoleMonitor：订阅 session/event，按记录类型分流打印三类关键事实
    * assistant/message ：推理内容(reasoning_content) + 正文 + 工具调用请求
    * tool/call         ：工具名 + 模型发起的原始 arguments(JSON 字符串)
    * turn/end          ：一轮结束（输出结束，可以继续对话）
  说明：assistant/message、tool/call、turn/end 三个「运行时事件」的 payload 目前
  是占位空壳（见 myagent/infra/events/payload.py），完整数据只落在 session 记录
  里；而 session 每次 append 都会触发 session/event，且 payload 携带刚落盘的记录。
  所以这里统一从 session/event 分流：既能监控这三类事实，也拿得到完整内容。
- main：读取控制台输入 -> agent.send 入队；/cancel 调 agent.cancel() 打断当前输出。

控制台输入：
    普通文本  作为用户消息发送（agent 正在输出时输入会先入队，本轮结束后执行）
    /cancel   取消当前在途 turn
    /exit     退出控制台

运行：
    python -m lifeprismevalue.agent.console
"""

from __future__ import annotations

import asyncio

from myagent.agent.core.session.types import (
    AssistantMessageData,
    ToolCallData,
    TurnEndData,
)
from myagent.infra.events.eventspec import SESSION_EVENT
from myagent.infra.events.payload import SessionEventPayload

from lifeprismevalue.agent.old_lifeprism_agent import create_old_agent

# 各模块输出之间的分割线：控制台里把不同事件的打印块隔开，便于查看
_SEP = "-" * 64


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

    def _print_turn_end(self, record) -> None:
        """turn/end：一轮结束，控制台可继续输入下一句。"""
        data: TurnEndData = record.data
        print(_SEP)
        print(
            f"[turn/end] turn={record.turn} "
            f"结束原因={data.reason_type} {data.reason_text}"
        )
        print(f"{_SEP}\n=== 输出结束，可以继续输入 ===")


async def _aio_input(prompt: str) -> str:
    """在 executor 里执行阻塞的 input()，避免卡住事件循环（agent 后台继续跑）。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


async def _main() -> None:
    agent = create_old_agent()
    # event_service 由 create_old_agent 内部创建并被 loop 持有；这里取来挂监控。
    # monitor 必须保持强引用（见 ConsoleMonitor.__init__ 的弱引用说明），
    # 它是 _main 的局部变量，函数存活期间不会被回收。
    monitor = ConsoleMonitor(agent._event_service)

    agent.start()
    print("=== lifeprism agent 控制台 ===")
    print("直接输入消息对话；/cancel 取消当前输出；/exit 退出")
    try:
        while True:
            try:
                line = (await _aio_input("\n你> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line == "/exit":
                break
            if line == "/cancel":
                agent.cancel()
                print("[console] 已取消当前输出，可继续输入")
                continue
            await agent.send(line)
    finally:
        agent.cancel()
        print("已退出控制台")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
