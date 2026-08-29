from typing import Any

from myagent.agent.core import Tool
from myagent.agent.tools.register import ToolRegister,ToolValueError
import pytest

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

    def execute(self, **kwargs) -> str:
        date = kwargs.get("date", None)
        return f"{date}的天气是晴天"

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