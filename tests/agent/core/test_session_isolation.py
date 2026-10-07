"""Session 持久化隔离测试。

命题：一个会话的 append 不得进入另一个会话的持久化。

本测试守护的失败形态——持久化经事件总线中转时（改造前）：

    A.append  → emit(SESSION_EVENT)                             session.py
              → 总线上 B 的 SessionPresist.cache_data 也被调用   persistence.py
              → B._buffer.append(A 的记录)
              → B.presist() 把 A 的记录写进 B 的文件

即 docs/known-limitations/2026-09-11-event-service无隔离机制.md 第 1 节举的第二个例子。
持久化改为 Session 自持并直连投喂后，总线不再承载落盘；本用例断言共享总线时该性质依然成立。

确定性来源：SessionPresist.loop() 首部有 asyncio.sleep，测试体内手工调用 presist()
观测，不依赖后台写循环。
"""
import pytest

from myagent.infra.events.service import EventService
from myagent.agent.core.provider import Message
from myagent.agent.core.session.session import Session
from myagent.agent.core.session.types import SessionMetaData, UserMessageData


@pytest.mark.asyncio
async def test_共享总线时_一个会话的append不进入另一个会话的持久化(tmp_path):
    """测试场景：两个会话绑同一份事件总线，A 追加记录后，B 的持久化不受影响

    观测点是落盘文件本身（外部可验证），不读 _buffer 等内部字段。
    """
    shared = EventService()
    path_a = tmp_path / "a.jsonl"
    path_b = tmp_path / "b.jsonl"
    session_a = Session(SessionMetaData(cwd=".", name="a"), [], session_file=path_a)
    session_b = Session(SessionMetaData(cwd=".", name="b"), [], session_file=path_b)
    session_a.bind(shared)
    session_b.bind(shared)

    try:
        session_a.append("user/message", UserMessageData(message=Message(role="user", content="A 的内容")))

        # A 自己的记录照常落盘——否则下面的断言会因为"谁都没写"而假绿
        session_a.presistence.presist()
        assert "A 的内容" in path_a.read_text(encoding="utf-8")

        # B 不该因共享总线而拿到 A 的记录：缓冲为空 → presist 不产出文件
        session_b.presistence.presist()
        assert not path_b.exists(), (
            f"B 的落盘文件被 A 的记录污染：{path_b.read_text(encoding='utf-8')}"
        )
    finally:
        await session_a.presistence.stop()
        await session_b.presistence.stop()
