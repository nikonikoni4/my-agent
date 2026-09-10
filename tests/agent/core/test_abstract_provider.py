from myagent.agent.core.provider import Message,RawToolCall
import pytest

@pytest.fixture
def correct_toolcallrequest():
    return RawToolCall(
        id = "123",
        name = "get_weather",
        arguments='{"location": "Paris, France"}',
    )
@pytest.fixture
def wrong_toolcallrequest():
    return RawToolCall(
        id = None,
        name = "get_weather",
        arguments='{"location": "Paris, France"}',
    )

def test_tool_to_dict(correct_toolcallrequest:RawToolCall,wrong_toolcallrequest:RawToolCall):
    """
    测试场景：
    1. 测试正确的to_dict
    2. 测试错误的to_dict
    """
    tool_dict = correct_toolcallrequest.to_dict()
    d = {
        "id" : "123",
        "type":"function",
        "function":{
            "name":"get_weather",
            # 契约：wire 格式中 arguments 是 JSON 字符串，不是 dict
            "arguments":'{"location": "Paris, France"}'
        }
    }
    assert tool_dict == d,"工具请求to_dict错误"
    with pytest.raises(ValueError):
        wrong_toolcallrequest.to_dict()


# ---------- Message.to_dict ----------

@pytest.fixture
def user_message():
    return Message(role="user", content="巴黎的天气怎么样？")

@pytest.fixture
def assistant_toolcall_message():
    return Message(
        role="assistant",
        content=None,
        tool_calls=[RawToolCall(id="call_1", name="get_weather", arguments='{"location": "Paris, France"}')],
    )

@pytest.fixture
def tool_result_message():
    return Message(role="tool", content='{"status": "ok"}', tool_call_id="call_1")


def test_user_message_to_dict(user_message:Message):
    """普通消息：只有 role 和 content，可选字段（tool_calls 等）不应出现在结果里"""
    assert user_message.to_dict() == {
        "role": "user",
        "content": "巴黎的天气怎么样？",
    }, "user 消息应只含 role 和 content 两个键"


def test_assistant_toolcall_message_to_dict(assistant_toolcall_message:Message):
    """assistant 发起工具调用：content 为 None 是合法的，arguments 序列化为 JSON 字符串"""
    d = assistant_toolcall_message.to_dict()
    assert d["role"] == "assistant"
    assert d["content"] is None
    assert d["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "get_weather",
                "arguments": '{"location": "Paris, France"}',
            },
        }
    ], "tool_calls 序列化应符合 OpenAI wire 格式，arguments 必须是 JSON 字符串"


def test_assistant_reasoning_content_to_dict():
    """reasoning_content 只有非 None 时才出现在结果里"""
    m = Message(role="assistant", content="答案是晴天", reasoning_content="用户问了天气，调用思考……")
    assert m.to_dict() == {
        "role": "assistant",
        "content": "答案是晴天",
        "reasoning_content": "用户问了天气，调用思考……",
    }, "reasoning_content 非空时应包含在结果中"


def test_tool_message_to_dict(tool_result_message:Message):
    """tool 结果消息：必须带 tool_call_id 与发起调用的 assistant 消息配对"""
    assert tool_result_message.to_dict() == {
        "role": "tool",
        "content": '{"status": "ok"}',
        "tool_call_id": "call_1",
    }, "tool 消息应含 role、content、tool_call_id 三个键"


def test_message_invalid_role_raises():
    """role 不在四个合法值中应报 ValueError"""
    with pytest.raises(ValueError):
        Message(role="assistant2", content="hi").to_dict()


def test_message_empty_role_raises():
    """role 为空字符串应报 ValueError"""
    with pytest.raises(ValueError):
        Message(role="", content="hi").to_dict()


def test_tool_message_without_call_id_raises():
    """tool 消息缺 tool_call_id 会导致 API 报 400，序列化时就应拦截"""
    with pytest.raises(ValueError):
        Message(role="tool", content="结果").to_dict()


def test_assistant_empty_without_toolcalls_raises():
    """assistant 消息 content 和 tool_calls 都为空，没有可发送的内容"""
    with pytest.raises(ValueError):
        Message(role="assistant", content=None).to_dict()


def test_user_message_with_none_content_raises():
    """只有 assistant 允许 content 为 None（发起工具调用时），其他角色不允许"""
    with pytest.raises(ValueError):
        Message(role="user", content=None).to_dict()