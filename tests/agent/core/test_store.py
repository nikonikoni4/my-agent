"""store 加载恢复测试。

往返链路：SessionPresist 落盘（首次写入自动补 meta 行 + chunk 固件归并 + 一条
user/message），再用 store.load 还原为 Session 并逐项断言。meta 行由 presist
的 meta_data 参数提供。store.load/create 只还原数据，不创建后台任务。
"""
import datetime
import json

from conftest import make_chunk_record_list
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
    store = SessionStore(tmp_path / "session")
    assert store.load("no-such-id", tmp_path / "proj") is None


def test_load_往返_解包chunk_还原嵌套消息(tmp_path):
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

    session = SessionStore(session_dir).load("s1", project_path)
    assert session is not None
    meta_restored = session.meta_data
    records = session.record_list
    # load 可在无事件循环环境下读取数据，事件与持久化留待显式绑定。
    assert session._event_service is None
    assert session.presistence is None

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


def test_create_返回未绑定空会话(tmp_path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    project_path = tmp_path / "proj"
    store = SessionStore(session_dir)

    session = store.create("新会话", project_path)

    assert session.record_list == []
    assert session.meta_data.name == "新会话"
    assert session.meta_data.cwd == str(project_path)
    assert session.presistence is None
    assert session._event_service is None
    # create 只计算路径，绑定前不创建项目编码子目录或文件。
    assert not store._session_file(session.meta_data.session_id, project_path).parent.exists()


def test_flat_session_folder即存储位置(tmp_path):
    """flat=True 时不编码 project_path，会话文件直接落在 session_folder 下。"""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    project_path = tmp_path / "proj"
    store = SessionStore(session_dir, flat=True)

    session = store.create("平铺会话", project_path)

    # 路径只有 session_folder 一层，不含项目编码子目录
    assert store._session_file(session.meta_data.session_id, project_path) == \
        session_dir / f"{session.meta_data.session_id}.jsonl"
    # 项目信息仍以 meta.cwd 为准，只是不参与定位
    assert session.meta_data.cwd == str(project_path)


def test_flat_load_与create同规则往返(tmp_path):
    """flat=True 时 load 与 create 用同一路径规则，写出的文件能被读回。"""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    project_path = tmp_path / "proj"
    path = session_dir / "s1.jsonl"

    meta = SessionMetaData(cwd="", session_id="s1", name="s1")
    comp = make_presist(path, meta)
    comp._buffer.append(SessionRecordData(
        type="user/message", seq=1, turn=1, step=1, surface_op="append",
        data=UserMessageData(message=Message(role="user", content="hi")),
    ))
    comp.presist()

    session = SessionStore(session_dir, flat=True).load("s1", project_path)

    assert session is not None
    assert session.meta_data.session_id == "s1"
    assert [r.type for r in session.record_list] == ["user/message"]
    assert session.record_list[0].data.message.content == "hi"


def test_meta_extra_自定义字段往返(tmp_path):
    """meta 的 extra 自定义字段：落盘后在首行原样写出，load 原样还原。"""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    path = session_dir / "s1.jsonl"

    meta = SessionMetaData(
        cwd="", session_id="s1", name="s1",
        extra={"run_id": "r-42", "tags": ["a", "b"], "nested": {"k": 1}},
    )
    comp = make_presist(path, meta)
    comp._buffer.append(SessionRecordData(
        type="user/message", seq=1, turn=1, step=1, surface_op="append",
        data=UserMessageData(message=Message(role="user", content="hi")),
    ))
    comp.presist()

    first_line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert first_line["extra"] == {"run_id": "r-42", "tags": ["a", "b"], "nested": {"k": 1}}
    # extra 是首行最后一个键：自定义字段不散在顶层，也不与内置字段争位
    assert list(first_line)[-1] == "extra"

    session = SessionStore(session_dir, flat=True).load("s1", tmp_path / "proj")
    assert session is not None
    assert session.meta_data.extra == {"run_id": "r-42", "tags": ["a", "b"], "nested": {"k": 1}}


def test_meta_extra_旧文件缺失或为null回落空dict(tmp_path):
    """旧版首行没有 extra 键、或显式写成 null 时，load 都回落为空 dict 而不报错。"""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    base = {
        "type": "meta_data", "format_version": 1, "cwd": "", "name": "legacy",
        "created_at": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc).isoformat(),
        "updated_at": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc).isoformat(),
        "parent_session_id": None,
    }
    # 旧版：整体没有 extra 行键
    (session_dir / "old.jsonl").write_text(
        json.dumps({**base, "session_id": "old"}, ensure_ascii=False) + "\n", encoding="utf-8")
    # 边界：extra 行键存在但为 null
    (session_dir / "null.jsonl").write_text(
        json.dumps({**base, "session_id": "null", "extra": None}, ensure_ascii=False) + "\n", encoding="utf-8")

    store = SessionStore(session_dir, flat=True)
    assert store.load("old", tmp_path / "proj").meta_data.extra == {}
    assert store.load("null", tmp_path / "proj").meta_data.extra == {}
