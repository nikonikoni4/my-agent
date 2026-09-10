import datetime

import pytest
from pathlib import Path

from myagent.agent.core.provider import Message, RawToolCall
from myagent.agent.core.session.deprecated_session import Session, SessionManager


# ---------- Session 基础行为 ----------

def test_session_auto_defaults():
    """测试场景：
    1. session_id 未指定时自动生成
    2. name 为空时在 __post_init__ 中回退为 session_id
    3. created_at 自动生成且带时区信息
    """
    s = Session()
    assert s.session_id, "session_id 应自动生成"
    assert s.name == s.session_id, "name 为空时应回退为 session_id"
    parsed = datetime.datetime.fromisoformat(s.created_at)
    assert parsed.tzinfo is not None, "created_at 应是带时区的 isoformat 时间"


def test_session_explicit_name():
    """测试场景：显式传入 name 时应保留，不回退"""
    s = Session(name="我的会话")
    assert s.name == "我的会话", "显式传入的 name 不应被覆盖"


def test_session_explicit_id_name_fallback():
    """测试场景：指定 session_id、name 为空时，name 回退为给定的 session_id"""
    s = Session(session_id="abc123")
    assert s.session_id == "abc123"
    assert s.name == "abc123", "name 为空时应回退为给定的 session_id"


def test_add_message():
    """测试场景：add_message 正常追加到 messages 列表"""
    s = Session()
    m = Message(role="user", content="你好")
    s.add_message(m)
    assert s.messages == [m], "消息应被追加到 messages 列表"


def test_add_message_invalid_type_raises():
    """测试场景：add_message 传入非 Message 类型应抛 TypeError"""
    s = Session()
    with pytest.raises(TypeError):
        s.add_message("这不是Message对象")


def test_meta_data_with_explicit_updated_at():
    """测试场景：meta_data 显式传入 updated_at 时使用传入值"""
    s = Session(
        session_id="id1",
        name="n1",
        last_compact_loc=3,
        created_at="2026-01-01T00:00:00+00:00",
    )
    d = s.meta_data(updated_at="2026-01-02T00:00:00+00:00")
    assert d == {
        "type": "meta_data",
        "session_id": "id1",
        "name": "n1",
        "last_compact_loc": 3,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-02T00:00:00+00:00",
    }, "meta_data 各字段应与传入值一致"


def test_meta_data_updated_at_fallback():
    """测试场景：不传 updated_at 时，优先用实例的 updated_at，为空则取当前时间"""
    s = Session()
    d = s.meta_data()
    assert d["updated_at"], "实例 updated_at 为空时应回退为当前时间"

    s2 = Session()
    s2.updated_at = "2026-01-02T00:00:00+00:00"
    assert s2.meta_data()["updated_at"] == "2026-01-02T00:00:00+00:00", \
        "实例已有 updated_at 时应优先使用"


# ---------- session_manager（内存部分，不涉及文件读写） ----------

def test_load_or_create_session_without_id():
    """测试场景：未指定 id 时每次新建会话，id 各不相同"""
    mgr = SessionManager(None)
    s1 = mgr.load_or_create_session()
    s2 = mgr.load_or_create_session()
    assert s1.session_id != s2.session_id, "未指定 id 时应各自新建会话"


def test_load_or_create_session_cache_hit():
    """测试场景：相同 session_id 再次取时应命中内存缓存，返回同一实例"""
    mgr = SessionManager(None)
    s1 = mgr.load_or_create_session("abc")
    s2 = mgr.load_or_create_session("abc")
    assert s1 is s2, "相同 id 应返回同一实例（内存缓存命中）"


def test_save_session_not_in_memory_raises():
    """测试场景：save_session 传入不在内存中的 id 应抛 KeyError"""
    mgr = SessionManager(None)
    with pytest.raises(KeyError):
        mgr.save_session("不存在的id")


@pytest.fixture
def new_session():
    session = Session(
        name="测试",
        messages=[
            Message("system","系统提示词"),
            Message("assistant","助手"),
            Message("user","user"),
            Message("assistant","",tool_calls=[
                RawToolCall("call_123","get_weather",'{"location": "shanghai"}')
            ]),
            Message("tool","测试结果",tool_call_id="call_123"),
            Message("assistant","输出结果")
        ]
    )
    return session 
    

@pytest.mark.skip(reason="废弃实现依赖旧版 Message.to_dict_with_timestamp API，provider 重构后未适配；deprecated_session 仅留作参考，不再维护")
def test_save_session_file(tmp_path:Path,new_session:Session):
    """
    测试场景：
    1. save_session 落盘后，文件存在，且第一行是 meta_data，后面每行是一条 Message。
    2. save_session 后用 _load_session_from_file 读回来，字段能完整还原（包括带 tool_calls 的消息）。
    3. load_or_create_session 传入一个已落盘的 id，返回的会话内容来自文件而不是新建。
    4. _load_session_from_file 传入不存在的路径，返回 None。
    5. get_session_id_from_files 返回目录下所有 jsonl 文件名去掉后缀
    """
    import json
    session_manager = SessionManager(session_file_path=tmp_path)
    session_manager._sessions[new_session.session_id] = new_session
    session_manager.save_session(new_session.session_id)
    session_file_path =  tmp_path / f"{new_session.session_id}.jsonl"
    assert session_file_path.exists(),"保存失败，无该文件"
            
    session_manager._sessions = {}
    session_from_file = session_manager._load_session_from_file(session_file_path)
    assert new_session.messages == session_from_file.messages, "保存的数据与读取的数据不一致"
    assert new_session == session_from_file, "保存的数据与读取的数据不一致"
    session_from_file = session_manager.load_or_create_session(new_session.session_id)
    assert new_session == session_from_file, "保存的数据与读取的数据不一致"

    session_from_file = session_manager._load_session_from_file(tmp_path / "123.json")
    assert session_from_file is None , "读取了不存在的文件"

    session_file_list = session_manager.get_session_id_from_files()
    assert new_session.session_id in session_file_list , "获取文件夹中的jsonl文件错误"


