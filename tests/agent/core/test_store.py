"""store 加载恢复测试。

往返链路：SessionPresist 落盘（首次写入自动补 meta 行 + chunk 固件归并 + 一条
user/message），再用 store.load 还原为 Session 并逐项断言。meta 行由 presist
的 meta_data 参数提供。store.load/create 内部构造 SessionPresist（依赖运行中
事件循环），测试体为 async。
"""
import datetime

import pytest

from conftest import make_chunk_record_list
from myagent.infra.events.service import EventService
from myagent.agent.core.session.store import SessionStore
from myagent.agent.core.session.persistence import SessionPresist
from myagent.agent.core.session.types import (
    SessionRecordData, UserMessageData, AssistantChunkData, SessionMetaData,
)
from myagent.agent.core.provider import Message
from myagent.utils.helper import project_path_to_session_folder


def make_presist(path, meta) -> SessionPresist:
    """绕过 __init__ 直接构造：避免 create_task 对运行中事件循环的依赖"""
    comp = SessionPresist.__new__(SessionPresist)
    comp.file_path = path
    comp.meta_data = meta
    comp._buffer = []
    comp._pending_truncate = None
    return comp


def test_load_文件不存在返回None(tmp_path):
    store = SessionStore(tmp_path / "session", EventService())
    assert store.load("no-such-id", tmp_path / "proj") is None


@pytest.mark.asyncio
async def test_load_往返_解包chunk_还原嵌套消息(tmp_path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    project_path = tmp_path / "proj"
    path = project_path_to_session_folder(project_path, session_dir) / "s1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    meta = SessionMetaData(cwd="", session_id="s1", name="s1")
    comp = make_presist(path, meta)
    user_rec = SessionRecordData(
        type="user/message", seq=11, turn=1, step=1, surface_op="append",
        data=UserMessageData(message=Message(role="user", content="hi")),
    )
    comp._buffer.extend([*make_chunk_record_list(), user_rec])
    comp.presist()

    session = SessionStore(session_dir, EventService()).load("s1", project_path)
    assert session is not None
    meta_restored = session.meta_data
    records = session.record_list
    # 组装断言：load 直接返回可用 Session，持久化组件随装配注入
    assert session._event_service is not None
    assert session.presistence is not None
    assert session.presistence.file_path == path

    # meta 行由 presist 首次写入自动补写，load 还原字段一致
    assert meta_restored.session_id == "s1"
    assert meta_restored.name == "s1"
    assert meta_restored.format_version == 1
    assert isinstance(meta_restored.created_at, datetime.datetime)

    # 总数：10 条 chunk 解包 + 1 条 user/message
    assert len(records) == 11
    assert records[-1].seq == 11

    # user/message：嵌套 Message 从 dict 还原为类型实例，surface_op 保留
    user_restored = records[-1]
    assert user_restored.type == "user/message"
    assert isinstance(user_restored.data, UserMessageData)
    assert isinstance(user_restored.data.message, Message)
    assert user_restored.data.message.content == "hi"
    assert user_restored.surface_op == "append"

    # chunk 解包：type/data 类型还原，seq 从 source_event_seqs 逐位恢复
    chunk_records = records[:10]
    assert [r.seq for r in chunk_records] == list(range(1, 11))
    assert all(r.type == "assistant/chunk" for r in chunk_records)
    assert all(isinstance(r.data, AssistantChunkData) for r in chunk_records)
    assert all(r.surface_op is None and r.source_event_seqs is None for r in chunk_records)

    # content 片段：texts 逐片还原
    assert [r.data.texts for r in chunk_records[:3]] == ["你", "好", "！"]

    # tool-call 首槽位：index 全员保留，id/name 只还原到组内首条
    tool_a = chunk_records[3:5]
    assert [r.data.index for r in tool_a] == [0, 0]
    assert tool_a[0].data.id == "call_a" and tool_a[1].data.id is None
    assert tool_a[0].data.name == "get_weather" and tool_a[1].data.name is None
    assert [r.data.args for r in tool_a] == ['{"date"', ': "2026-09-05"}']
    # 并行调用的第二槽位独立还原
    assert chunk_records[6].data.index == 1
    assert chunk_records[6].data.id == "call_b" and chunk_records[6].data.name == "get_time"
    assert chunk_records[6].data.args == "{}"

    # finish 片段：结束原因走独立 finish_reason 字段，texts 保持 None
    finish = chunk_records[9]
    assert finish.data.type == "finish"
    assert finish.data.finish_reason == "stop"
    assert finish.data.texts is None

    # timestamp 按 组首 + dt 累积重建：固件相邻间隔 1ms，逐条恢复后应与原始一致
    for i, r in enumerate(chunk_records):
        assert r.timestamp == datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(milliseconds=i + 1)
        # 片段 uuid 不落盘（身份按 seq 位置还原，参照 DeepSeek-Harness），恢复时重新生成
        assert r.uuid


@pytest.mark.asyncio
async def test_create_返回空会话并绑定持久化(tmp_path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    project_path = tmp_path / "proj"
    store = SessionStore(session_dir, EventService())

    session = store.create("新会话", project_path)

    assert session.record_list == []
    assert session.meta_data.name == "新会话"
    assert session.meta_data.cwd == str(project_path)
    assert session.presistence is not None
    # 文件路径在项目编码子文件夹下，meta 行随首批记录才写入，此时文件尚不存在
    assert session.presistence.file_path == project_path_to_session_folder(project_path, session_dir) / f"{session.meta_data.session_id}.jsonl"
    assert not session.presistence.file_path.exists()
