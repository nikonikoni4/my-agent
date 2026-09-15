import pytest

from myagent.infra.events.service import EventService
from myagent.agent.core.provider import ChatParams, Message, Usage
from myagent.agent.core.session.session import Session
from myagent.agent.core.session.types import (
    SessionMetaData,
    SessionRecordData,
    UserMessageData,
    AssistantMessageData,
    RequestHeaderData,
    TurnStartData,
    StepStartData,
    TurnEndData,
    CompactionEndData,
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
def make_session(event_service, meta_data):
    """工厂固件：构造 Session。

    SessionPresist 构造时需要运行中的事件循环，所以必须在
    async 测试体内部调用本工厂，不能在 fixture 里直接构造。
    """
    def _make(record_list: list[SessionRecordData] | None = None) -> Session:
        return Session(event_service, meta_data, record_list)
    return _make


@pytest.fixture
def add_exchange(make_session):
    """工厂固件：返回一个初始化函数，在测试体内调用得到 (session, 追加一轮对话的函数)。

    每轮写入 turn/start（不进可见面）+ user/message + assistant/message（surface_op="append"）。
    data 只带事件内容，turn/step 由 Session 写在信封上。
    Session 必须在事件循环内构造，所以测试要先调用本工厂再操作。
    """
    def _setup():
        session = make_session()

        def _add(user_content: str, assistant_content: str):
            session.append("turn/start", TurnStartData(), None, None)
            session.append("user/message",
                           UserMessageData(message=Message(role="user", content=user_content)),
                           "append", None)
            session.append("assistant/message",
                           AssistantMessageData(message=Message(role="assistant", content=assistant_content)),
                           "append", None)
        return session, _add
    return _setup


@pytest.fixture
def compact_range():
    """工厂固件：向会话追加一条 compaction/summary 记录，用 replace surface_op 遮蔽一段消息。

    start/end 是被遮蔽消息的 seq 闭区间，source_event_seqs 必须与区间内实际 seq 一致。
    """
    def _compact(session, start: int, end: int, shadowed_seqs: list[int], summary: str = "摘要"):
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

    @pytest.mark.asyncio
    async def test_fresh_session_state(self, make_session, meta_data, event_service):
        """测试场景：空会话的初始状态——无记录、turn/step 归零、注入对象原样保存"""
        session = make_session()
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

    @pytest.mark.asyncio
    async def test_init_with_loaded_records(self, make_session):
        """测试场景：带历史记录构造时 turn 恢复为信封上的 turn，node 同步还原"""
        records = [
            SessionRecordData(type="turn/start", seq=1, turn=3, surface_op=None,
                              data=TurnStartData()),
            SessionRecordData(type="user/message", seq=2, turn=3, step=1, surface_op="append",
                              data=UserMessageData(message=Message(role="user", content="hi"))),
        ]
        s = make_session(records)
        assert s.turn == 3
        assert s.surface_manager.node == [2]

    @pytest.mark.asyncio
    async def test_init_skips_turn_none_records(self, make_session):
        """测试场景：最后一条是轮间压缩记录（信封 turn=None）时，向前找最近一条带 turn 的记录恢复"""
        records = [
            SessionRecordData(type="turn/start", seq=1, turn=1, step=None, data=TurnStartData()),
            SessionRecordData(type="turn/end", seq=2, turn=1, step=None,
                              data=TurnEndData(reason_type="success", reason_text="", error_type="")),
            SessionRecordData(type="compaction/end", seq=3, turn=None, step=None,
                              data=CompactionEndData(compaction_id="c-1")),
        ]
        s = make_session(records)
        assert s.turn == 1


class TestSessionAppend:
    """测试 append 的写入边界：校验、seq 分配、序列化。"""

    @pytest.mark.asyncio
    async def test_append_unknown_event_type_raises(self, make_session):
        """测试场景：event_type 不在登记处时抛 ValueError"""
        session = make_session()
        with pytest.raises(ValueError):
            session.append("no/such", TurnStartData(), None, None)

    @pytest.mark.asyncio
    async def test_append_wrong_data_type_raises(self, make_session):
        """测试场景：data 类型与 event_type 登记的不符时抛 TypeError"""
        session = make_session()
        with pytest.raises(TypeError):
            session.append("turn/start",
                           UserMessageData(message=Message(role="user", content="x")),
                           None, None)

    @pytest.mark.asyncio
    async def test_append_assigns_sequential_seq(self, make_session):
        """测试场景：seq 从 1 开始按写入顺序连续递增"""
        session = make_session()
        session.append("turn/start", TurnStartData(), None, None)
        session.append("step/start", StepStartData(), None, None)
        assert [r.seq for r in session.record_list] == [1, 2]
        assert session.record_list_seq == 2

    @pytest.mark.asyncio
    async def test_append_keeps_typed_data(self, add_exchange):
        """测试场景：data 以类型实例落库（不做 asdict），uuid/timestamp 自动填充，surface_op 原样保存"""
        session, add = add_exchange()
        add("hi", "hello")
        record = session.record_list[1]  # user/message
        assert record.type == "user/message"
        assert isinstance(record.data, UserMessageData)
        assert record.data.message.role == "user"
        assert record.data.message.content == "hi"
        assert record.surface_op == "append"
        assert record.source_event_seqs is None
        assert record.uuid
        assert record.timestamp is not None

    @pytest.mark.asyncio
    async def test_to_record_dict_puts_turn_step_on_envelope(self, add_exchange):
        """测试场景：序列化后 turn/step 在记录顶层，data 里只有事件内容、不含定位字段"""
        session, add = add_exchange()
        add("hi", "hello")
        d = session.record_list[1].to_record_dict()
        assert d["turn"] == 1
        assert d["step"] == 0
        assert "turn" not in d["data"]
        assert "step" not in d["data"]


class TestTurnStepTracking:
    """测试 turn/step 坐标跟踪：定位信息统一写在信封上。"""

    @pytest.mark.asyncio
    async def test_turn_start_increments_turn_and_resets_step(self, add_exchange):
        """测试场景：每次 turn/start 使 turn 加一、step 归零"""
        session, add = add_exchange()
        add("u1", "a1")
        assert session.turn == 1
        assert session.step == 0
        add("u2", "a2")
        assert session.turn == 2
        assert session.step == 0

    @pytest.mark.asyncio
    async def test_step_start_increments_step(self, make_session):
        """测试场景：同一 turn 内 step/start 使 step 递增"""
        session = make_session()
        session.append("turn/start", TurnStartData(), None, None)
        session.append("step/start", StepStartData(), None, None)
        session.append("step/start", StepStartData(), None, None)
        assert session.turn == 1
        assert session.step == 2

    @pytest.mark.asyncio
    async def test_envelope_positions_for_full_turn(self, make_session):
        """测试场景：一整轮内各类事件的信封坐标——

        turn 边界事件（turn/start、turn/end）step 记 None；
        step/start 前的事件 step 记 0；step 内事件记当前 step。
        """
        session = make_session()
        session.append("turn/start", TurnStartData(), None, None)
        session.append("user/message",
                       UserMessageData(message=Message(role="user", content="hi")),
                       "append", None)
        session.append("step/start", StepStartData(), None, None)
        session.append("assistant/message",
                       AssistantMessageData(message=Message(role="assistant", content="ok")),
                       "append", None)
        session.append("step/start", StepStartData(), None, None)
        session.append("turn/end", TurnEndData(reason_type="success", reason_text="", error_type=""), None, None)

        got = [(r.type, r.turn, r.step) for r in session.record_list]
        assert got == [
            ("turn/start", 1, None),
            ("user/message", 1, 0),
            ("step/start", 1, 1),
            ("assistant/message", 1, 1),
            ("step/start", 1, 2),
            ("turn/end", 1, None),
        ]

    @pytest.mark.asyncio
    async def test_data_does_not_carry_position(self, add_exchange):
        """测试场景：data 不再保存 turn/step，定位只在信封上"""
        session, add = add_exchange()
        add("hi", "hello")
        for record in session.record_list:
            assert not hasattr(record.data, "turn")
            assert not hasattr(record.data, "step")

    @pytest.mark.asyncio
    async def test_compaction_events_step_is_none(self, add_exchange, compact_range):
        """测试场景：压缩事件的 step 记 None，turn 记当前轮"""
        session, add = add_exchange()
        add("u1", "a1")  # seq 1-3, 当前轮 1
        compact_range(session, start=2, end=3, shadowed_seqs=[2, 3])
        record = session.record_list[-1]
        assert record.type == "compaction/summary"
        assert record.turn == 1
        assert record.step is None


class TestResumeTurn:
    """测试从历史记录恢复会话后的坐标延续。"""

    @pytest.mark.asyncio
    async def test_resume_continues_turn_numbering(self, add_exchange):
        """测试场景：用已有记录重建会话后，下一轮 turn/start 从上次轮次继续递增"""
        session, add = add_exchange()
        add("u1", "a1")
        resumed = Session(session._event_service, session.meta_data, session.record_list)
        resumed.append("turn/start", TurnStartData(), None, None)
        assert resumed.record_list[-1].turn == 2

    @pytest.mark.asyncio
    async def test_resume_resets_step_to_zero(self, make_session):
        """测试场景：恢复时 step 不还原，从 0 重新计（步骤坐标只在单次运行内有效）"""
        records = [
            SessionRecordData(type="step/start", seq=1, turn=1, step=3,
                              data=StepStartData()),
        ]
        s = make_session(records)
        assert s.turn == 1
        assert s.step == 0


class TestDeriveMessages:
    """测试 derive_messages：从可见面还原 LLM 输入用的消息序列。"""

    @pytest.mark.asyncio
    async def test_derive_empty_session(self, make_session):
        """测试场景：空会话派发出空列表"""
        session = make_session()
        assert session.derive_messages() == []

    @pytest.mark.asyncio
    async def test_derive_returns_visible_messages_in_order(self, add_exchange):
        """测试场景：只有 user/assistant 消息进可见面，turn/start 被排除，顺序与写入一致，返回 Message 对象"""
        session, add = add_exchange()
        add("u1", "a1")
        add("u2", "a2")
        messages = session.derive_messages()
        assert all(isinstance(m, Message) for m in messages)
        assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]
        assert [m.content for m in messages] == ["u1", "a1", "u2", "a2"]

    @pytest.mark.asyncio
    async def test_derive_excludes_compacted_messages(self, add_exchange, compact_range):
        """测试场景：replace 压缩后，被遮蔽区间的消息不再派发，区间外消息保留"""
        session, add = add_exchange()
        add("u1", "a1")   # seq 1=turn/start, 2=user, 3=assistant
        add("u2", "a2")   # seq 4=turn/start, 5=user, 6=assistant
        compact_range(session, start=2, end=3, shadowed_seqs=[2, 3])  # seq 7
        messages = session.derive_messages()
        assert [m.content for m in messages] == ["u2", "a2"]

    @pytest.mark.asyncio
    async def test_derive_after_compaction_appends_new_messages(self, add_exchange, compact_range):
        """测试场景：压缩之后再追加的新消息正常进可见面"""
        session, add = add_exchange()
        add("u1", "a1")
        compact_range(session, start=2, end=3, shadowed_seqs=[2, 3])
        add("u2", "a2")
        messages = session.derive_messages()
        assert [m.content for m in messages] == ["u2", "a2"]


class TestLatestRequestHeader:
    """测试 latest_request_header：读取最近一条 request/header 配置快照。"""

    @staticmethod
    def _header(reason, model_name, system_prompt="sp", tools=None, params=None):
        return RequestHeaderData(
            reason=reason,
            model_name=model_name,
            system_prompt=system_prompt,
            tools=tools if tools is not None else [],
            params=params,
        )

    @pytest.mark.asyncio
    async def test_empty_session_returns_none(self, make_session):
        """测试场景：新会话从未写入 request/header，返回 None"""
        session = make_session()
        assert session.latest_request_header() is None

    @pytest.mark.asyncio
    async def test_returns_header_data_with_all_fields(self, make_session):
        """测试场景：写入一条后返回其 data，reason/model_name/system_prompt/tools/params 原样可读"""
        session = make_session()
        session.append("request/header",
                       self._header("initial", "m-1", system_prompt="sp-1",
                                    tools=[{"name": "get_weather"}],
                                    params=ChatParams(temperature=0.7, max_tokens=1024)),
                       None, None)
        header = session.latest_request_header()
        assert isinstance(header, RequestHeaderData)
        assert header.reason == "initial"
        assert header.model_name == "m-1"
        assert header.system_prompt == "sp-1"
        assert header.tools == [{"name": "get_weather"}]
        assert header.params == ChatParams(temperature=0.7, max_tokens=1024)

    @pytest.mark.asyncio
    async def test_returns_latest_when_multiple(self, make_session):
        """测试场景：写入 initial 后又写入 change，返回最近一条（从后往前找）"""
        session = make_session()
        session.append("request/header", self._header("initial", "m-1"), None, None)
        session.append("request/header", self._header("change", "m-2", system_prompt="sp-2"),
                       None, None)
        header = session.latest_request_header()
        assert header.reason == "change"
        assert header.model_name == "m-2"
        assert header.system_prompt == "sp-2"

    @pytest.mark.asyncio
    async def test_skips_interleaved_other_records(self, make_session):
        """测试场景：快照后继续追加其他类型记录，查询跳过它们仍返回原快照"""
        session = make_session()
        session.append("request/header", self._header("initial", "m-1"), None, None)
        session.append("user/message",
                       UserMessageData(message=Message(role="user", content="hi")),
                       "append", None)
        session.append("assistant/message",
                       AssistantMessageData(message=Message(role="assistant", content="ok")),
                       "append", None)
        assert session.latest_request_header().model_name == "m-1"

    @pytest.mark.asyncio
    async def test_loaded_history_returns_last(self, make_session):
        """测试场景：从落盘历史恢复（record_list 直接构造）后返回最后一条快照"""
        records = [
            SessionRecordData(type="request/header", seq=1, turn=1, step=0,
                              data=self._header("initial", "m-1")),
            SessionRecordData(type="request/header", seq=2, turn=2, step=0,
                              data=self._header("change", "m-2")),
        ]
        session = make_session(records)
        assert session.latest_request_header().reason == "change"
        assert session.latest_request_header().model_name == "m-2"


class TestLlmRetryCount:
    """测试 llm_retry_count：按 turn 统计 llm/retry 记录条数。

    llm/retry 事件类型尚未在 RECORD_DATA_TYPES 注册（append 会拒绝写入），
    测试用手工构造的信封记录验证查询逻辑本身；等类型注册后可改走 append。
    """

    @staticmethod
    def _retry_record(seq: int, turn: int | None, step: int = 1) -> SessionRecordData:
        # data 用 StepStartData 占位：查询只看信封上的 type/turn，不看 data 内容
        return SessionRecordData(type="llm/retry", seq=seq, turn=turn, step=step,
                                 data=StepStartData())

    @pytest.mark.asyncio
    async def test_empty_session_returns_zero(self, make_session):
        """测试场景：空会话任何 turn 的重试计数都是 0"""
        session = make_session()
        assert session.llm_retry_count(1) == 0

    @pytest.mark.asyncio
    async def test_counts_only_requested_turn(self, make_session):
        """测试场景：turn 1 有 2 条、turn 2 有 1 条，各查各的，互不串数"""
        records = [
            self._retry_record(1, turn=1),
            self._retry_record(2, turn=1),
            self._retry_record(3, turn=2),
        ]
        session = make_session(records)
        assert session.llm_retry_count(1) == 2
        assert session.llm_retry_count(2) == 1

    @pytest.mark.asyncio
    async def test_missing_turn_returns_zero(self, make_session):
        """测试场景：查询不存在的 turn 返回 0（该轮没有记录或轮号没用过）"""
        records = [self._retry_record(1, turn=1)]
        session = make_session(records)
        assert session.llm_retry_count(5) == 0

    @pytest.mark.asyncio
    async def test_ignores_other_types_in_same_turn(self, make_session):
        """测试场景：同 turn 内的其他类型记录不计入，只数 type 为 llm/retry 的"""
        records = [
            SessionRecordData(type="turn/start", seq=1, turn=1, step=None,
                              data=TurnStartData()),
            SessionRecordData(type="step/start", seq=2, turn=1, step=1,
                              data=StepStartData()),
            self._retry_record(3, turn=1),
        ]
        session = make_session(records)
        assert session.llm_retry_count(1) == 1

    @pytest.mark.asyncio
    async def test_ignores_turn_none_records(self, make_session):
        """测试场景：信封 turn=None 的 llm/retry（轮间压缩位置）不归属任何轮"""
        records = [self._retry_record(1, turn=None, step=None)]
        session = make_session(records)
        assert session.llm_retry_count(0) == 0
        assert session.llm_retry_count(1) == 0


class TestCompactNotImplemented:
    """测试 compact 占位行为。"""

    @pytest.mark.asyncio
    async def test_compact_is_placeholder(self, make_session):
        """测试场景：compact 尚未实现，调用不报错也不产生记录"""
        session = make_session()
        session.compact()
        assert session.record_list == []
