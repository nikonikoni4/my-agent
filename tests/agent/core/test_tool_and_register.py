from typing import Any

from myagent.agent.core import Tool
from myagent.agent.core.provider import RawToolCall
from myagent.agent.core.tool.register import ToolRegister
from myagent.agent.core.tool.tool import ParsedToolCall, ToolResult, ToolErrorType
from myagent.agent.execption import ToolValueError, ToolConsecutiveFailureError
import pytest

def call(name: str, arguments: str | dict = "{}", call_id: str = "call_1") -> RawToolCall:
    """构造一次模型原始工具调用。

    arguments 为 str 表示 provider 未解析成功（wire 原样 JSON 字符串，含坏 JSON），
    为 dict 表示 provider 已解析成功——两形态凭类型区分，见 RawToolCall。
    """
    return RawToolCall(id=call_id, name=name, arguments=arguments)

class WeatherTool(Tool):
    def __init__(self):
        super().__init__()

    @property
    def name(self) -> str:
        return "get_weather"

    @property
    def description(self) -> str:
        return "获取某天的天气状况"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "需要查询的日期，格式YYYY-MM-DD"
                }
            }
        }

    async def execute(self, **kwargs) -> str:
        date = kwargs.get("date", None)
        return f"{date}的天气是晴天"


class AnotherTool(Tool):
    """无参数工具，用于多工具注册场景"""

    @property
    def name(self) -> str:
        return "get_time"

    @property
    def description(self) -> str:
        return "获取当前时间"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return "12:00"
ALLOWED_TYPES = {"string", "number", "integer", "boolean", "array", "object"}


def assert_valid_parameter_schema(schema: dict, path: str) -> None:
    """递归校验参数 schema 是否符合 OpenAI function call 规范"""
    assert isinstance(schema, dict), f"{path} 必须是 dict"
    assert schema.get("type") in ALLOWED_TYPES, f"{path}.type 非法: {schema.get('type')}"
    assert isinstance(schema.get("description"), str) and schema["description"], f"{path} 缺少 description"

    if schema["type"] == "object":
        props = schema.get("properties")
        assert isinstance(props, dict) and props, f"{path} 是 object 但 properties 非法"
        for key, sub in props.items():
            assert_valid_parameter_schema(sub, f"{path}.properties.{key}")
        for name in schema.get("required", []):
            assert name in props, f"{path}.required 引用了不存在的字段 {name}"

    if schema["type"] == "array":
        assert "items" in schema, f"{path} 是 array 但缺少 items"
        assert_valid_parameter_schema(schema["items"], f"{path}.items")

    if "enum" in schema:
        assert isinstance(schema["enum"], list) and schema["enum"], f"{path}.enum 必须是非空列表"


def test_tool_schemas():
    tool = WeatherTool()
    schema = tool.to_schema()

    # 顶层结构
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "get_weather"
    assert fn["description"] == "获取某天的天气状况"

    # parameters 顶层必须是 object（OpenAI 硬性要求）
    params = fn["parameters"]
    assert isinstance(params, dict), "parameters 必须是 dict"
    assert params.get("type") == "object", "parameters 顶层必须是 object schema"
    assert "properties" in params, "parameters 缺少 properties"

    # 递归校验每个参数的结构
    for key, sub in params["properties"].items():
        assert_valid_parameter_schema(sub, f"parameters.properties.{key}")

    # required 里的字段必须存在于 properties
    for name in params.get("required", []):
        assert name in params["properties"], f"required 引用了不存在的字段 {name}"



@pytest.fixture
def register():
    return ToolRegister()
    

def test_tool_register(register:ToolRegister):
    """测试场景：
    1. 工具成功注册
    2. 工具注册幂等性
    3. 注册非法工具
    4. 正常卸载工具
    5. 非法卸载不存在的工具不会抛出错误
    """
    register.register(WeatherTool())
    assert WeatherTool().name in register.tool_list(), "注册失败"
    register.register(WeatherTool())
    assert len(register.tool_list()) ==1 , "注册两个相同的工具幂等性失败"

    class IllegalTool(Tool):
        @property
        def name(self) -> str:
            return ""

        @property
        def description(self) -> str:
            return ""

        @property
        def parameters(self) -> dict[str, Any]:
            return {}
            

        def execute(self, **kwargs) -> str:
            return 
               
    with pytest.raises(ToolValueError):
        register.register(IllegalTool())

    register.unregister(WeatherTool().name)
    assert WeatherTool().name not in register.tool_list(), "注销失败"

    with pytest.raises(ToolValueError):
        register.unregister(1)
    
    register.unregister("123")


def test_to_schemas(register: ToolRegister):
    """测试场景：
    1. 注册表为空时返回空列表
    2. 注册多个工具后返回对应数量的 schema
    3. 每个 schema 内容与工具自身的 to_schema() 一致
    """
    # 空注册表
    assert register.to_schemas() == []

    # 注册多个工具后，schema 数量与工具数量一致
    tool1 = WeatherTool()
    tool2 = AnotherTool()
    register.register([tool1, tool2])
    schemas = register.to_schemas()
    assert len(schemas) == 2, "schema 数量应与注册的工具数量一致"

    # 每个 schema 与对应工具的 to_schema() 输出一致
    expected = {tool1.name: tool1.to_schema(), tool2.name: tool2.to_schema()}
    for schema in schemas:
        name = schema["function"]["name"]
        assert schema == expected[name], f"工具 {name} 的 schema 与 to_schema() 不一致"


# ---------------------------------------------------------------------------
# 参数 JSON 解析（信任边界：模型输出的 arguments 不可信，解析在工具层）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_非法JSON返回错误结果_不执行工具但计入熔断(register: ToolRegister):
    """模型输出单引号/括号不匹配等非法 JSON：返回带 hint 的错误 ToolResult，
    工具本体不执行；解析失败与执行失败同等计入熔断（熔断防的是模型反复
    调用同一工具一直出错，模型侧写坏参数也算）"""
    tool = FlakyTool(max_consecutive_failures=5)
    register.register(tool)

    result = await register.execute(call("flaky", '{"date": "2026-09-02"'))  # 缺右括号

    assert result.is_error is True
    assert result.error_type is ToolErrorType.PARSE_ERROR
    assert "不是合法 JSON" in result.content
    assert '{"date": "2026-09-02"' in result.content, "原文应回显给模型定位错误"
    assert "hint" in result.content
    assert tool.calls == 0, "解析失败不应执行工具本体"
    assert register._consecutive_failures == {"flaky": 1}, "解析失败应计入熔断计数"


@pytest.mark.asyncio
async def test_连续JSON解析失败同样触发熔断(register: ToolRegister):
    """模型反复用非法 JSON 调同一工具：连续解析失败达到阈值即熔断，
    防止坏参数调用无限循环"""
    tool = FlakyTool(max_consecutive_failures=5)
    register.register(tool)

    for _ in range(4):
        result = await register.execute(call("flaky", '{"date": "2026'))
        assert result.is_error is True and result.error_type is ToolErrorType.PARSE_ERROR
    assert register.to_schemas() == [tool.to_schema()], "4 < 5，不应熔断"
    assert tool.calls == 0, "工具本体从未执行"

    result = await register.execute(call("flaky", '{"date": "2026'))
    assert register.to_schemas() == [], "连续第 5 次解析失败应熔断"
    assert "已熔断" in result.content, "触发熔断的当次结果附 hint"


@pytest.mark.asyncio
async def test_JSON解析结果非dict返回错误结果(register: ToolRegister):
    """arguments 是合法 JSON 但不是对象（如数组）：返回带 hint 的错误结果"""
    register.register(WeatherTool())

    result = await register.execute(call("get_weather", '[1, 2]'))

    assert result.is_error is True
    assert result.error_type is ToolErrorType.PARSE_NOT_OBJECT
    assert "不是 JSON 对象" in result.content or "JSON 对象" in result.content


@pytest.mark.asyncio
async def test_空串参数按空对象处理正常执行(register: ToolRegister):
    """arguments 为空串：按 "{}" 处理，无参数工具正常执行"""
    register.register(AnotherTool())

    result = await register.execute(call("get_time", ""))

    assert result.is_error is False
    assert result.content == "12:00"


# ---------------------------------------------------------------------------
# arguments 两形态分流（provider 已解析成 dict 时直通，工具层不再解析）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_已解析的dict参数直通执行(register: ToolRegister):
    """provider 解析成功时 arguments 已是 dict：工具层直接使用，不重新解析"""
    register.register(WeatherTool())

    result = await register.execute(call("get_weather", {"date": "2026-09-19"}))

    assert result.is_error is False
    assert "2026-09-19" in result.content


def test_parse_call_对已解析调用直接产出ParsedToolCall(register: ToolRegister):
    """类型即标志：非 str 必然是 dict，不解析也不回喂错误"""
    parsed = register.parse_call(call("get_weather", {"date": "2026-09-19"}))

    assert isinstance(parsed, ParsedToolCall)
    assert parsed.arguments == {"date": "2026-09-19"}
    assert parsed.tool_name == "get_weather"
    assert parsed.call_id == "call_1"


@pytest.mark.asyncio
async def test_已解析的dict参数校验失败仍走工具层校验(register: ToolRegister):
    """跳过解析不等于跳过校验：必填/未知参数仍由工具层判定"""
    register.register(WeatherTool())

    result = await register.execute(call("get_weather", {"unknown": 1}))

    assert result.is_error is True
    assert result.error_type is ToolErrorType.PARAM_VALIDATION


# ---------------------------------------------------------------------------
# 熔断（连续失败计数 / 触发 / 拦截 / 清零，规格见 架构设计/工具调用.md）
# ---------------------------------------------------------------------------


class FlakyTool(Tool):
    """失败可控的工具：fail=True 时 execute 抛异常，否则成功。"""

    def __init__(self, **breaker_kwargs):
        super().__init__(**breaker_kwargs)
        self.fail = True
        self.calls = 0

    @property
    def name(self) -> str:
        return "flaky"

    @property
    def description(self) -> str:
        return "测试熔断的可控失败工具"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"第{self.calls}次失败")
        return ToolResult(content="成功")


def test_熔断阈值下限校验():
    """max_consecutive_failures：None 表示不熔断（step_limit 兜底）；设置时必须 >= 5"""
    FlakyTool()  # None，合法
    FlakyTool(max_consecutive_failures=5)  # 恰为下限，合法
    with pytest.raises(ValueError):
        FlakyTool(max_consecutive_failures=4)


@pytest.mark.asyncio
async def test_未配置熔断_失败不计数不熔断(register: ToolRegister):
    """max_consecutive_failures=None：连续失败既不计数也不熔断"""
    tool = FlakyTool()
    register.register(tool)
    for _ in range(8):
        result = await register.execute(call("flaky"))
        assert result.is_error is True
    # 未熔断：schema 保留；不计数：失败表为空
    assert register.to_schemas() == [tool.to_schema()]
    assert register._consecutive_failures == {}


@pytest.mark.asyncio
async def test_连续失败达阈值_熔断且当次结果附hint(register: ToolRegister):
    """schema_hide（默认）：第 5 次失败触发熔断，当次结果附 hint，之后 schema 过滤"""
    tool = FlakyTool(max_consecutive_failures=5)
    register.register(tool)

    # 前 4 次：普通错误结果，无熔断字样
    for _ in range(4):
        result = await register.execute(call("flaky"))
        assert result.is_error is True
        assert "已熔断" not in result.content

    # 第 5 次：触发熔断，当次结果附 hint
    result = await register.execute(call("flaky"))
    assert result.is_error is True
    assert "已熔断" in result.content
    assert "请改用其他工具" in result.content

    # schema_hide：熔断后从 schema 消失
    assert register.to_schemas() == []


@pytest.mark.asyncio
async def test_成功一次清零连续计数(register: ToolRegister):
    """失败->失败->成功：计数清零，需重新连续失败满阈值才熔断"""
    tool = FlakyTool(max_consecutive_failures=5)
    register.register(tool)

    await register.execute(call("flaky"))  # 失败 1
    await register.execute(call("flaky"))  # 失败 2
    tool.fail = False
    await register.execute(call("flaky"))  # 成功，清零
    tool.fail = True
    for _ in range(4):
        await register.execute(call("flaky"))  # 重新连续失败 4 次
    assert register.to_schemas() == [tool.to_schema()], "4 < 5，不应熔断"

    await register.execute(call("flaky"))  # 重新连续第 5 次失败
    assert register.to_schemas() == [], "连续第 5 次失败应熔断"


@pytest.mark.asyncio
async def test_turn_end清空熔断_下一turn恢复可用(register: ToolRegister):
    """reset_breaker（turn/end 回调）：清空计数与熔断表，工具恢复"""
    tool = FlakyTool(max_consecutive_failures=5)
    register.register(tool)
    for _ in range(5):
        await register.execute(call("flaky"))
    assert register.to_schemas() == []

    register.reset_breaker()

    assert register.to_schemas() == [tool.to_schema()]
    assert register._tripped == set()


@pytest.mark.asyncio
async def test_熔断即抛错_raise_on_break(register: ToolRegister):
    """raise_on_break=True：触发熔断当次抛 ToolConsecutiveFailureError（人在回路入口）"""
    tool = FlakyTool(max_consecutive_failures=5, raise_on_break=True)
    register.register(tool)

    for _ in range(4):
        await register.execute(call("flaky"))  # 未达阈值，正常返回错误结果

    with pytest.raises(ToolConsecutiveFailureError):
        await register.execute(call("flaky"))  # 第 5 次触发熔断并抛错
    assert register._tripped == {"flaky"}


@pytest.mark.asyncio
async def test_execute_intercept模式_熔断后入口驳回(register: ToolRegister):
    """execute_intercept：熔断后 schema 保留，入口驳回且不再执行工具本体"""
    tool = FlakyTool(max_consecutive_failures=5, breaker_mode="execute_intercept")
    register.register(tool)

    for _ in range(5):
        await register.execute(call("flaky"))

    # schema 不被过滤（与 schema_hide 的区别）
    assert register.to_schemas() == [tool.to_schema()]

    # 熔断后调用：入口驳回，返回错误结果，工具本体不执行
    calls_before = tool.calls
    result = await register.execute(call("flaky"))
    assert result.is_error is True
    assert "已因连续失败" in result.content
    assert tool.calls == calls_before


@pytest.mark.asyncio
async def test_execute返回str包装为成功ToolResult(register: ToolRegister):
    """子类 execute 返回 str：register 包装为 is_error=False 的 ToolResult"""
    register.register(WeatherTool())
    result = await register.execute(call("get_weather", '{"date": "2026-09-02"}'))
    assert isinstance(result, ToolResult)
    assert result.is_error is False
    assert result.content == "2026-09-02的天气是晴天"
