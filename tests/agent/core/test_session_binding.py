"""Session 数据加载与运行绑定分离的契约测试。"""
import asyncio
import json

import pytest

from myagent.agent.core.provider import Message
from myagent.agent.core.session.session import Session
from myagent.agent.core.session.store import SessionStore
from myagent.agent.core.session.types import SessionMetaData, UserMessageData
from myagent.agent.execption import SessionReBindError
from myagent.infra.events.eventspec import SESSION_EVENT
from myagent.infra.events.service import EventService


def append_message(session: Session, content: str) -> None:
    session.append("user/message", UserMessageData(Message(role="user", content=content)), "append")


async def stop_persistence(session: Session) -> None:
    if session.presistence is not None:
        task = session.presistence._presist_loop
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_store_create_无事件循环也能访问数据且不创建持久化(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = store.create("data-only", tmp_path / "project")
    append_message(session, "仅内存")

    assert session.derive_messages()[0].content == "仅内存"
    assert session.presistence is None
    assert not (tmp_path / "sessions").exists()


def test_store_load_无事件循环还原消息且不修改文件(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    project = tmp_path / "project"
    source = Session(SessionMetaData(cwd=str(project), session_id="history"))
    append_message(source, "历史记录")
    path = store._session_file("history", project)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(source.meta_data.meta_data()) + "\n" +
                    json.dumps(source.record_list[0].to_record_dict()) + "\n", encoding="utf-8")
    before = path.read_bytes()

    restored = store.load("history", project)

    assert restored is not None
    assert restored.derive_messages()[0].content == "历史记录"
    assert restored.presistence is None
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_bind_首次创建持久化且仅广播绑定后的事件(tmp_path):
    session = SessionStore(tmp_path / "sessions").create("bind", tmp_path / "project")
    append_message(session, "绑定前")
    events = []

    def observe(payload):
        events.append(payload)

    bus = EventService()
    bus.register(SESSION_EVENT.name, observe)
    session.bind(bus)
    try:
        assert session.presistence is not None
        assert events == []
        append_message(session, "绑定后")
        session.presistence.presist()
        lines = session.presistence.file_path.read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["data"]["message"]["content"] for line in lines[1:]] == ["绑定前", "绑定后"]
        assert len(events) == 1
    finally:
        await stop_persistence(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_bus", [True, False])
async def test_bind_重复绑定抛错且不更换总线或持久化(tmp_path, same_bus):
    session = SessionStore(tmp_path / "sessions").create("repeat", tmp_path / "project")
    original, other = EventService(), EventService()
    received_original, received_other = [], []

    def on_original(payload):
        received_original.append(payload)

    def on_other(payload):
        received_other.append(payload)

    original.register(SESSION_EVENT.name, on_original)
    other.register(SESSION_EVENT.name, on_other)
    session.bind(original)
    persistence = session.presistence
    tasks = asyncio.all_tasks()
    try:
        with pytest.raises(SessionReBindError):
            session.bind(original if same_bus else other)
        assert session.presistence is persistence
        assert asyncio.all_tasks() == tasks
        append_message(session, "仍归原总线")
        assert len(received_original) == 1
        assert received_other == []
    finally:
        await stop_persistence(session)


@pytest.mark.asyncio
async def test_load_bind_不重写历史但保存绑定前后新增记录(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    project = tmp_path / "project"
    original = store.create("history", project)
    original.bind(EventService())
    try:
        append_message(original, "历史")
        original.presistence.presist()
    finally:
        await stop_persistence(original)

    restored = store.load(original.meta_data.session_id, project)
    append_message(restored, "加载后绑定前")
    restored.bind(EventService())
    try:
        append_message(restored, "绑定后")
        restored.presistence.presist()
        records = [json.loads(line) for line in restored.presistence.file_path.read_text(encoding="utf-8").splitlines()[1:]]
        assert [record["seq"] for record in records] == [1, 2, 3]
        assert [record["data"]["message"]["content"] for record in records] == ["历史", "加载后绑定前", "绑定后"]
    finally:
        await stop_persistence(restored)


def test_bind_内存会话只接入事件且重复绑定也抛错():
    session = Session(SessionMetaData(cwd="."))
    received = []

    def observe(payload):
        received.append(payload)

    bus = EventService()
    bus.register(SESSION_EVENT.name, observe)
    session.bind(bus)
    with pytest.raises(SessionReBindError):
        session.bind(bus)
    append_message(session, "内存会话")

    assert session.presistence is None
    assert len(received) == 1
