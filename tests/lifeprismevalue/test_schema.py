"""注册与 schema 校验。

验证全部移植工具可被 myagent 的 ToolRegister 注册，且其参数 schema 符合
OpenAI function call 规范（myagent 参数校验依赖该规范）。
"""

from __future__ import annotations

from myagent.agent.core.tool.register import ToolRegister

from lifeprismevalue.tools import _LIFEPRISM_TOOL_CLASSES, build_lifeprism_tools

ALLOWED_TYPES = {"string", "number", "integer", "boolean", "array", "object"}


def _resolve_type(t):
    """解析 JSON Schema type：可为单字符串或联合类型列表（取第一个非 null）。"""
    if isinstance(t, list):
        for item in t:
            if item != "null":
                return item
        return None
    return t


def assert_valid_parameter_schema(schema: dict, path: str) -> None:
    """递归校验参数 schema 是否符合 OpenAI function call 规范。

    只校验 type 合法、object 含 properties、array 含 items（对症 myagent 参数
    校验依赖的结构）；description 为提示性字段，允许缺失（不少原生 schema 的
    object 根节点无 description）。
    """
    assert isinstance(schema, dict), f"{path} 必须是 dict"
    rtype = _resolve_type(schema.get("type"))
    if rtype is None:
        # 未声明 type 的自由节点（如过滤 value），允许
        return
    assert rtype in ALLOWED_TYPES, f"{path}.type 非法: {schema.get('type')}"

    if rtype == "object":
        props = schema.get("properties")
        # 自由对象可用 additionalProperties 代替固定 properties
        if props is not None:
            assert isinstance(props, dict), f"{path}.properties 必须是 dict"
            for name, sub in props.items():
                assert_valid_parameter_schema(sub, f"{path}.properties.{name}")
    elif rtype == "array":
        items = schema.get("items")
        assert isinstance(items, dict), f"{path}.items 必须是 dict"
        assert_valid_parameter_schema(items, f"{path}.items")
    elif rtype == "string":
        assert isinstance(schema.get("enum", []), list), f"{path}.enum 必须是 list"


def test_all_lifeprism_tools_count() -> None:
    """应移植 19 个工具（13 个数据类 + 6 个文件系统）。"""
    assert len(_LIFEPRISM_TOOL_CLASSES) == 19


def test_build_register_no_duplicate_and_valid() -> None:
    """工具可全部注册，工具名唯一且 schema 合法。"""
    tools = build_lifeprism_tools()
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), f"存在重复工具名: {names}"

    register = ToolRegister()
    register.register(tools)
    assert len(register.tool_list()) == 19

    for t in tools:
        schema = t.to_schema()
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] == t.name
        assert fn["description"]
        # 首层必须是 object
        assert fn["parameters"]["type"] == "object"


def test_registered_schemas_all_valid() -> None:
    """注册表 to_schemas() 产出的 schema 全部符合并校验规范。"""
    register = ToolRegister()
    register.register(build_lifeprism_tools())
    schemas = register.to_schemas()
    assert len(schemas) == 19
    for s in schemas:
        assert_valid_parameter_schema(s["function"]["parameters"], s["function"]["name"])