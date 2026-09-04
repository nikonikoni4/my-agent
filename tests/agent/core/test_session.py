import pytest

from myagent.infra.events.service import EventService
from myagent.agent.core.provider import Message, Usage
from myagent.agent.core.session.session import Session
from myagent.agent.core.session.types import (
    SessionMetaData,
    SessionRecordData,
    UserMessageData,
    AssistantMessageData,
    TurnStartData,
    StepStartData,
    CompactionSummaryData,
)


# ---------------------------------------------------------------------------
# 测试固件
# ---------------------------------------------------------------------------

@pytest.fixture
def event_service():
    """每个测试独立的空事件服务，Session 构造必需。"""
    return EventService()


@pytest.fixture
def meta_data():
    """固定 cwd 与 name 的会话元信息，session_id 随机生成。"""
    return SessionMetaData(cwd=".", name="test-session")


@pytest.fixture
def session(event_service, meta_data):
    """空会话：没有任何记录，turn=0、step=0。"""
    return Session(event_service, meta_data)


@pytest.fixture
def add_exchange(session):
    """工厂固件：向会话追加一轮完整对话并返回追加函数，可连续调用追加多轮。

    每轮写入 turn/start（不进可见面）+ user/message + assistant/message（surface_op="append"）。
    """
    def _add(user_content: str, assistant_content: str):
        session.append("turn/start", TurnStartData(turn=1), None, None)
        session.append("user/message",
                       UserMessageData(turn=1, step=1,
                                       message=Message(role="user", content=user_content)),
                       "append", None)
        session.append("assistant/message",
                       AssistantMessageData(turn=1, step=1,
                                            message=Message(role="assistant", content=assistant_content)),
                       "append", None)
    return _add


@pytest.fixture
def compact_range(session):
    """工厂固件：追加一条 compaction/summary 记录，用 replace surface_op 遮蔽一段消息。

    start/end 是被遮蔽消息的 seq 闭区间，source_event_seqs 必须与区间内实际 seq 一致。
    """
    def _compact(start: int, end: int, shadowed_seqs: list[int], summary: str = "摘要"):
        session.append("compaction/summary",
                       CompactionSummaryData(
                           compaction_id="c-1",
                           summary=summary,
                           shadowed_seqs=shadowed_seqs,
                           shadowed_token_count=100,
                           model_name="test-model",
                           usage=Usage(),
                       ),
                       {"op": "replace", "start": start, "end": end},
                       shadowed_seqs)
    return _compact


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

class TestSessionInit:
    """测试 Session 构造与初始状态。"""

    def test_fresh_session_state(self, session, meta_data, event_service):
        """测试场景：空会话的初始状态——无记录、turn/step 归零、注入对象原样保存"""
        assert session.record_list == []
        assert session.record_list_seq == 0
        assert session.turn == 0
        assert session.step == 0
        assert session.surface_manager.node == []
        assert session.meta_data is meta_data
        assert session._event_service is event_service

    def test_meta_data_is_required(self, event_service):
        """测试场景：meta_data 必填（cwd 是必填字段），缺省构造直接报 TypeError"""
        with pytest.raises(TypeError):
            Session(event_service)

    def test_init_with_loaded_records(self, event_service, meta_data):
        """测试场景：带历史记录构造时 turn 恢复为最后一条记录的 turn，node 同步还原"""
        records = [
            SessionRecordData(type="turn/start", seq=1, surface_op=None, data=TurnStartData(turn=3)),
            SessionRecordData(type="user/message", seq=2, surface_op="append",
                              data=UserMessageData(turn=3, step=1,
                                                   message=Message(role="user", content="hi"))),
        ]
        s = Session(event_service, meta_data, records)
        assert s.turn == 3
        assert s.surface_manager.node == [2]


class TestSessionAppend:
    """测试 append 的写入边界：校验、seq 分配、序列化。"""

    def test_append_unknown_event_type_raises(self, session):
        """测试场景：event_type 不在登记处时抛 ValueError"""
        with pytest.raises(ValueError):
            session.append("no/such", TurnStartData(turn=1), None, None)

    def test_append_wrong_data_type_raises(self, session):
        """测试场景：data 类型与 event_type 登记的不符时抛 TypeError"""
        with pytest.raises(TypeError):
            session.append("turn/start",
                           UserMessageData(turn=1, step=1,
                                           message=Message(role="user", content="x")),
                           None, None)

    def test_append_assigns_sequential_seq(self, session):
        """测试场景：seq 从 1 开始按写入顺序连续递增"""
        session.append("turn/start", TurnStartData(turn=1), None, None)
        session.append("step/start", StepStartData(turn=1, step=1), None, None)
        assert [r.seq for r in session.record_list] == [1, 2]
        assert session.record_list_seq == 2

    def test_append_keeps_typed_data(self, session, add_exchange):
        """测试场景：data 以类型实例落库（不做 asdict），uuid/timestamp 自动填充，surface_op 原样保存"""
        add_exchange("hi", "hello")
        record = session.record_list[1]  # user/message
        assert record.type == "user/message"
        assert isinstance(record.data, UserMessageData)
        assert record.data.message.role == "user"
        assert record.data.message.content == "hi"
        assert record.surface_op == "append"
        assert record.source_event_seqs is None
        assert record.uuid
        assert record.timestamp is not None


class TestTurnStepTracking:
    """测试 turn/step 坐标跟踪。"""

    def test_turn_start_increments_turn_and_resets_step(self, session, add_exchange):
        """测试场景：每次 turn/start 使 turn 加一、step 归零"""
        add_exchange("u1", "a1")
        assert session.turn == 1
        assert session.step == 0
        add_exchange("u2", "a2")
        assert session.turn == 2
        assert session.step == 0

    def test_step_start_increments_step(self, session):
        """测试场景：同一 turn 内 step/start 使 step 递增"""
        session.append("turn/start", TurnStartData(turn=1), None, None)
        session.append("step/start", StepStartData(turn=1, step=1), None, None)
        session.append("step/start", StepStartData(turn=1, step=2), None, None)
        assert session.turn == 1
        assert session.step == 2

    def test_turn_step_in_data_overridden_by_session(self, session):
        """测试场景：data 里自带的 turn/step 被会话当前坐标覆盖，且不改动调用方传入的原对象"""
        session.append("turn/start", TurnStartData(turn=1), None, None)
        caller_data = UserMessageData(turn=99, step=99,
                                      message=Message(role="user", content="hi"))
        session.append("user/message", caller_data, "append", None)
        record = session.record_list[1]
        assert record.data.turn == 1
        assert record.data.step == 0  # turn/start 后 step 重置为 0
        assert record.data is not caller_data  # replace 生成了副本
        assert caller_data.turn == 99  # 原对象未被改动


class TestDeriveMessages:
    """测试 derive_messages：从可见面还原 LLM 输入用的消息序列。"""

    def test_derive_empty_session(self, session):
        """测试场景：空会话派发出空列表"""
        assert session.derive_messages() == []

    def test_derive_returns_visible_messages_in_order(self, session, add_exchange):
        """测试场景：只有 user/assistant 消息进可见面，turn/start 被排除，顺序与写入一致，返回 Message 对象"""
        add_exchange("u1", "a1")
        add_exchange("u2", "a2")
        messages = session.derive_messages()
        assert all(isinstance(m, Message) for m in messages)
        assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]
        assert [m.content for m in messages] == ["u1", "a1", "u2", "a2"]

    def test_derive_excludes_compacted_messages(self, session, add_exchange, compact_range):
        """测试场景：replace 压缩后，被遮蔽区间的消息不再派发，区间外消息保留"""
        add_exchange("u1", "a1")   # seq 1=turn/start, 2=user, 3=assistant
        add_exchange("u2", "a2")   # seq 4=turn/start, 5=user, 6=assistant
        compact_range(start=2, end=3, shadowed_seqs=[2, 3])  # seq 7
        messages = session.derive_messages()
        assert [m.content for m in messages] == ["u2", "a2"]

    def test_derive_after_compaction_appends_new_messages(self, session, add_exchange, compact_range):
        """测试场景：压缩之后再追加的新消息正常进可见面"""
        add_exchange("u1", "a1")
        compact_range(start=2, end=3, shadowed_seqs=[2, 3])
        add_exchange("u2", "a2")
        messages = session.derive_messages()
        assert [m.content for m in messages] == ["u2", "a2"]


class TestCompactNotImplemented:
    """测试 compact 占位行为。"""

    def test_compact_is_placeholder(self, session):
        """测试场景：compact 尚未实现，调用不报错也不产生记录"""
        session.compact()
        assert session.record_list == []
