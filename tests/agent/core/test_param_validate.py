"""工具调用参数校验的单元测试。

覆盖 ToolRegister 三个校验环节：
1. _validate_required_parameters —— 必填字段校验
2. _validate_param_value     —— 单参数类型归一化 + 值校验
3. _validate_param           —— 整组参数归一化

聚焦"模型可能以字符串形式传入非字符串参数"的场景，覆盖每种类型的
正常归一化、保持原样、以及各约束关键字命中/未命中的分支。
"""
from typing import Any

import pytest

from myagent.agent.core.tool.register import ToolRegister
from myagent.agent.core.tool.tool import Tool, ToolResult
from myagent.agent.execption import ToolValidateParameterError


@pytest.fixture
def register():
    return ToolRegister()


# ---------------------------------------------------------------------------
# _validate_required_parameters
# ---------------------------------------------------------------------------

class TestRequiredParameters:
    def test_no_required_returns(self, register):
        """schema 无 required（None）时不抛异常"""
        schema = {"properties": {"a": {"type": "string"}}}
        assert register._validate_required_parameters(schema, {"a": "x"}) is None

    def test_empty_required_returns(self, register):
        """required 为空列表时不抛异常"""
        schema = {"required": []}
        assert register._validate_required_parameters(schema, {}) is None

    def test_all_required_present(self, register):
        """required 声明的字段全部存在时不抛异常"""
        schema = {"properties": {"a": {}, "b": {}}, "required": ["a", "b"]}
        assert register._validate_required_parameters(schema, {"a": 1, "b": 2}) is None

    def test_missing_required_raises(self, register):
        """缺失 required 中声明的字段时抛异常，且 message 带字段名"""
        schema = {"properties": {"a": {}, "b": {}}, "required": ["a", "b"]}
        with pytest.raises(ToolValidateParameterError,
                           match="缺少必要参数b"):
            register._validate_required_parameters(schema, {"a": 1})

    def test_missing_among_multiple(self, register):
        """多个必填字段只缺一个时，报缺失的那个"""
        schema = {"properties": {"a": {}, "b": {}, "c": {}}, "required": ["a", "b", "c"]}
        with pytest.raises(ToolValidateParameterError, match="b"):
            register._validate_required_parameters(schema, {"a": 1, "c": 3})


# ---------------------------------------------------------------------------
# _validate_param_value —— 类型归一化
# ---------------------------------------------------------------------------

class TestBooleanNormalization:
    @pytest.mark.parametrize("raw", ["true", "True", "1"])
    def test_string_true_variants(self, register, raw):
        """布尔参数以 'true'/'True'/'1' 字符串传入 → True"""
        assert register._validate_param_value({"type": "boolean"}, raw) is True

    @pytest.mark.parametrize("raw", ["false", "False", "0", "yes", "2", ""])
    def test_string_false_variants(self, register, raw):
        """其他字符串传入布尔参数 → False"""
        assert register._validate_param_value({"type": "boolean"}, raw) is False

    @pytest.mark.parametrize("raw", [True, False])
    def test_real_bool_unchanged(self, register, raw):
        """已是真的 bool → 原样返回"""
        assert register._validate_param_value({"type": "boolean"}, raw) is raw

    @pytest.mark.parametrize("raw", [0, 1, 2.0, None])
    def test_non_string_scalar_unchanged(self, register, raw):
        """布尔 schema 但传非 str 标量 → 不转换，原样返回"""
        assert register._validate_param_value({"type": "boolean"}, raw) is raw


class TestIntegerNormalization:
    def test_string_int(self, register):
        """整型参数以 '42' 字符串传入 → 42 (int)"""
        val = register._validate_param_value({"type": "integer"}, "42")
        assert val == 42
        assert isinstance(val, int)

    def test_real_int_unchanged(self, register):
        """已是 int → 原样返回"""
        assert register._validate_param_value({"type": "integer"}, 42) is 42

    def test_float_unchanged(self, register):
        """integer schema 但传入 float → 不做截断，原样返回"""
        assert register._validate_param_value({"type": "integer"}, 4.5) is 4.5

    def test_invalid_string_raises_value_error(self, register):
        """非数字字符串转 int 失败 → 抛 ValueError（由调用方包装）"""
        with pytest.raises(ValueError):
            register._validate_param_value({"type": "integer"}, "abc")


class TestNumberNormalization:
    @pytest.mark.parametrize("raw,expected", [("3.14", 3.14), ("42", 42.0), ("-1.5", -1.5)])
    def test_string_number(self, register, raw, expected):
        """number 参数以字符串传入 → float"""
        val = register._validate_param_value({"type": "number"}, raw)
        assert val == expected
        assert isinstance(val, float)

    def test_real_float_unchanged(self, register):
        """已是 float → 原样返回"""
        assert register._validate_param_value({"type": "number"}, 3.14) is 3.14

    def test_int_unchanged(self, register):
        """number schema 但传入 int → 原样返回（int 是合法 number）"""
        assert register._validate_param_value({"type": "number"}, 42) is 42

    def test_invalid_string_raises_value_error(self, register):
        """非数字字符串转 float 失败 → 抛 ValueError"""
        with pytest.raises(ValueError):
            register._validate_param_value({"type": "number"}, "abc")


class TestStringPreserved:
    def test_string_unchanged(self, register):
        """string 参数 → 原样返回，不做转换"""
        assert register._validate_param_value({"type": "string"}, "42") == "42"

    def test_text_string(self, register):
        assert register._validate_param_value({"type": "string"}, "晴天") == "晴天"


class TestArrayObjectPreserved:
    def test_array_unchanged(self, register):
        """array 参数 → 原样返回"""
        raw = ["a", "b"]
        assert register._validate_param_value({"type": "array"}, raw) is raw

    def test_object_unchanged(self, register):
        """object 参数 → 原样返回"""
        raw = {"k": "v"}
        assert register._validate_param_value({"type": "object"}, raw) is raw


class TestNonDictSchema:
    def test_schema_not_dict(self, register):
        """schema 不是 dict 时直接原样返回值"""
        val = register._validate_param_value(None, "whatever")
        assert val == "whatever"


# ---------------------------------------------------------------------------
# _validate_param_value —— 值校验（约束关键字）
# ---------------------------------------------------------------------------

class TestEnumChecking:
    def test_value_in_enum_pass(self, register):
        assert register._validate_param_value(
            {"type": "string", "enum": ["auto", "manual"]}, "auto") == "auto"

    def test_value_not_in_enum_raises(self, register):
        with pytest.raises(ToolValidateParameterError, match="不在枚举"):
            register._validate_param_value(
                {"type": "string", "enum": ["auto", "manual"]}, "other")

    def test_enum_applied_after_normalization(self, register):
        """enum 校验在归一化之后：'1' 被转成 1，需命中 int enum"""
        assert register._validate_param_value(
            {"type": "integer", "enum": [1, 2]}, "1") == 1

    def test_no_enum_skips(self, register):
        """schema 无 enum 时不做枚举校验"""
        assert register._validate_param_value({"type": "string"}, "anything") == "anything"


class TestStringLengthChecking:
    @pytest.mark.parametrize("value", ["ab", "abcd", "abcde"])
    def test_within_min_length(self, register, value):
        """长度 >= minLength 通过"""
        assert register._validate_param_value(
            {"type": "string", "minLength": 2}, value) == value

    def test_below_min_length_raises(self, register):
        with pytest.raises(ToolValidateParameterError, match="minLength"):
            register._validate_param_value({"type": "string", "minLength": 3}, "ab")

    @pytest.mark.parametrize("value", ["", "a", "abc"])
    def test_within_max_length(self, register, value):
        """长度 <= maxLength 通过"""
        assert register._validate_param_value(
            {"type": "string", "maxLength": 3}, value) == value

    def test_above_max_length_raises(self, register):
        with pytest.raises(ToolValidateParameterError, match="maxLength"):
            register._validate_param_value({"type": "string", "maxLength": 3}, "abcd")

    def test_no_length_skips(self, register):
        """未设置长度限制时不校验"""
        assert register._validate_param_value({"type": "string"}, "x" * 100) == "x" * 100


class TestNumericRangeChecking:
    @pytest.mark.parametrize("schema,kwargs", [
        ({"type": "integer", "minimum": 1}, {"value": 1}),
        ({"type": "number", "minimum": 1.5}, {"value": 1.5}),
    ])
    def test_at_minimum_pass(self, register, schema, kwargs):
        assert register._validate_param_value(schema, kwargs["value"]) == kwargs["value"]

    def test_below_minimum_raises(self, register):
        with pytest.raises(ToolValidateParameterError, match="minimum"):
            register._validate_param_value({"type": "integer", "minimum": 1}, 0)

    def test_at_maximum_pass(self, register):
        assert register._validate_param_value({"type": "integer", "maximum": 10}, 10) == 10

    def test_above_maximum_raises(self, register):
        with pytest.raises(ToolValidateParameterError, match="maximum"):
            register._validate_param_value({"type": "number", "maximum": 1.0}, 1.5)

    def test_range_checked_after_normalization(self, register):
        """范围校验作用于归一化之后的值"""
        assert register._validate_param_value(
            {"type": "integer", "minimum": 1, "maximum": 5}, "3") == 3
        with pytest.raises(ToolValidateParameterError, match="maximum"):
            register._validate_param_value(
                {"type": "integer", "maximum": 5}, "7")

    def test_no_range_skips(self, register):
        assert register._validate_param_value({"type": "integer"}, 999) == 999

    def test_boolean_not_counted_as_number(self, register):
        """bool 不算数值范围校验对象（nor ra时为绕过排除逻辑的回归保护）"""
        val = register._validate_param_value({"type": "boolean", "enum": [True]}, "true")
        assert val is True


class TestArrayItemsChecking:
    def test_within_min_items(self, register):
        assert register._validate_param_value(
            {"type": "array", "minItems": 2}, [1, 2]) == [1, 2]

    def test_below_min_items_raises(self, register):
        with pytest.raises(ToolValidateParameterError, match="minItems"):
            register._validate_param_value({"type": "array", "minItems": 2}, [1])

    def test_within_max_items(self, register):
        assert register._validate_param_value(
            {"type": "array", "maxItems": 2}, [1, 2]) == [1, 2]

    def test_above_max_items_raises(self, register):
        with pytest.raises(ToolValidateParameterError, match="maxItems"):
            register._validate_param_value({"type": "array", "maxItems": 2}, [1, 2, 3])

    def test_no_items_limit_skips(self, register):
        assert register._validate_param_value({"type": "array"}, [1] * 10) == [1] * 10


# ---------------------------------------------------------------------------
# _validate_param —— 整组参数
# ---------------------------------------------------------------------------

class TestValidateParam:
    def test_no_properties_returns_input(self, register):
        """schema 无 properties 时原样返回入参"""
        params = {"a": 1}
        result = register._validate_param({"type": "object"}, params)
        assert result == params

    def test_normalizes_all_params(self, register):
        """混合多种类型参数一次性归一化"""
        schema = {
            "properties": {
                "flag": {"type": "boolean"},
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "name": {"type": "string"},
            }
        }
        raw = {"flag": "true", "count": "5", "ratio": "3.14", "name": "晴"}
        result = register._validate_param(schema, raw)
        assert result == {"flag": True, "count": 5, "ratio": 3.14, "name": "晴"}
        assert isinstance(result["flag"], bool)
        assert isinstance(result["count"], int)
        assert isinstance(result["ratio"], float)

    def test_does_not_mutate_input(self, register):
        """deepcopy 保证不修改入参 dict"""
        schema = {"properties": {"flag": {"type": "boolean"}}}
        raw = {"flag": "true"}
        register._validate_param(schema, raw)
        assert raw == {"flag": "true"}

    def test_unknown_parameter_raises(self, register):
        """传入未在 schema 声明的参数 → 抛异常"""
        schema = {"properties": {"known": {"type": "string"}}}
        with pytest.raises(ToolValidateParameterError, match="未知参数extra"):
            register._validate_param(schema, {"known": "x", "extra": 1})

    def test_conversion_failure_wrapped(self, register):
        """归一化失败（ValueError）被包装为 ToolValidateParameterError"""
        schema = {"properties": {"count": {"type": "integer"}}}
        with pytest.raises(ToolValidateParameterError, match="真实值为"):
            register._validate_param(schema, {"count": "not-a-number"})

    def test_constraint_failure_propagates(self, register):
        """约束违反的 ToolValidateParameterError 原样抛出"""
        schema = {"properties": {"n": {"type": "integer", "maximum": 5}}}
        with pytest.raises(ToolValidateParameterError, match="maximum"):
            register._validate_param(schema, {"n": "10"})

    def test_empty_parameters(self, register):
        """空参数 dict 通过"""
        schema = {"properties": {"a": {"type": "integer"}}}
        assert register._validate_param(schema, {}) == {}

    def test_bool_not_misdetected_as_missing(self, register):
        """布尔假值等边界值也能正确归一化写回 vp"""
        schema = {"properties": {"flag": {"type": "boolean"}}}
        result = register._validate_param(schema, {"flag": "0"})
        assert result == {"flag": False}


# ---------------------------------------------------------------------------
# 数值开区间 / 倍数 / 数组唯一性
# ---------------------------------------------------------------------------

class TestExclusiveRangeChecking:
    @pytest.mark.parametrize("value", [0.5, 9, 5.5])
    def test_exclusive_minimum_exclusive_maximum_pass(self, register, value):
        """同时满足 0 < value < 10 通过"""
        assert register._validate_param_value(
            {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 10},
            value) == value

    def test_equal_to_exclusive_minimum_raises(self, register):
        """value == exclusiveMinimum 时排除（严格大于）"""
        with pytest.raises(ToolValidateParameterError, match="exclusiveMinimum"):
            register._validate_param_value(
                {"type": "number", "exclusiveMinimum": 0}, 0)

    def test_below_exclusive_minimum_raises(self, register):
        with pytest.raises(ToolValidateParameterError, match="exclusiveMinimum"):
            register._validate_param_value(
                {"type": "number", "exclusiveMinimum": 0}, -1)

    def test_equal_to_exclusive_maximum_raises(self, register):
        """value == exclusiveMaximum 时排除（严格小于）"""
        with pytest.raises(ToolValidateParameterError, match="exclusiveMaximum"):
            register._validate_param_value(
                {"type": "number", "exclusiveMaximum": 10}, 10)

    def test_above_exclusive_maximum_raises(self, register):
        with pytest.raises(ToolValidateParameterError, match="exclusiveMaximum"):
            register._validate_param_value(
                {"type": "number", "exclusiveMaximum": 10}, 10.5)

    def test_boundary_checks_after_normalization(self, register):
        """开区间校验作用于归一化之后的值"""
        assert register._validate_param_value(
            {"type": "integer", "exclusiveMinimum": 0}, "1") == 1
        with pytest.raises(ToolValidateParameterError):
            register._validate_param_value(
                {"type": "integer", "exclusiveMinimum": 0}, "0")

    def test_no_exclusive_skips(self, register):
        assert register._validate_param_value({"type": "number"}, 0) == 0


class TestMultipleOfChecking:
    @pytest.mark.parametrize("schema,value", [
        ({"type": "integer", "multipleOf": 5}, 15),
        ({"type": "integer", "multipleOf": 5}, 10),
        ({"type": "number", "multipleOf": 0.1}, 0.3),
    ])
    def test_is_multiple_pass(self, register, schema, value):
        assert register._validate_param_value(schema, value) == value

    def test_not_multiple_raises(self, register):
        with pytest.raises(ToolValidateParameterError, match="整数倍"):
            register._validate_param_value({"type": "integer", "multipleOf": 5}, 12)

    def test_number_fraction_multiple_raises(self, register):
        with pytest.raises(ToolValidateParameterError, match="整数倍"):
            register._validate_param_value({"type": "number", "multipleOf": 0.1}, 0.25)

    def test_multiple_checked_after_normalization(self, register):
        assert register._validate_param_value(
            {"type": "integer", "multipleOf": 3}, "9") == 9
        with pytest.raises(ToolValidateParameterError):
            register._validate_param_value(
                {"type": "integer", "multipleOf": 3}, "10")

    def test_no_multiple_of_skips(self, register):
        assert register._validate_param_value({"type": "integer"}, 7) == 7


class TestUniqueItemsChecking:
    def test_unique_items_pass(self, register):
        assert register._validate_param_value(
            {"type": "array", "uniqueItems": True}, [1, 2, 3]) == [1, 2, 3]

    def test_duplicate_items_raises(self, register):
        with pytest.raises(ToolValidateParameterError, match="重复元素"):
            register._validate_param_value(
                {"type": "array", "uniqueItems": True}, [1, 2, 2])

    def test_unique_items_false_allows_duplicates(self, register):
        """uniqueItems 显式为 false 时允许重复"""
        assert register._validate_param_value(
            {"type": "array", "uniqueItems": False}, [1, 2, 2]) == [1, 2, 2]

    def test_no_unique_items_skips(self, register):
        assert register._validate_param_value(
            {"type": "array"}, ["a", "a"]) == ["a", "a"]


# ---------------------------------------------------------------------------
# execute 全链路：校验在工具执行前生效
# ---------------------------------------------------------------------------

import json

from myagent.agent.core.tool.tool import Tool


class FullTypeTool(Tool):
    """覆盖 string / integer / number / boolean / array 五种参数类型的工具。

    execute 把收到的 kwargs 原样转 JSON 返回，便于断言归一化后的真实类型。
    """

    @property
    def name(self) -> str:
        return "full_type_tool"

    @property
    def description(self) -> str:
        return "覆盖全部参数类型的测试工具"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["mode", "count"],
            "properties": {
                "mode": {"type": "string", "enum": ["fast", "slow"]},
                "count": {"type": "integer", "minimum": 1, "maximum": 10},
                "ratio": {"type": "number"},
                "verbose": {"type": "boolean"},
                "tags": {"type": "array", "uniqueItems": True},
            },
        }

    async def execute(self, **kwargs) -> str:
        return json.dumps(kwargs, ensure_ascii=False)


def make_full_type_register(tool=None) -> ToolRegister:
    register = ToolRegister()
    register.register(tool or FullTypeTool())
    return register


class TestExecuteWithValidation:
    @pytest.mark.asyncio
    async def test_valid_params_executes_normally(self):
        """全部参数合法且归一化后，正常执行工具"""
        register = make_full_type_register()
        raw = {"mode": "fast", "count": "3", "ratio": "2.5",
               "verbose": "true", "tags": ["a", "b"]}
        result = await register.execute("full_type_tool", **raw)
        assert result.is_error is False
        data = json.loads(result.content)
        # 类型已归一化：字符串转回对应基础类型
        assert data["count"] == 3 and isinstance(data["count"], int)
        assert data["ratio"] == 2.5 and isinstance(data["ratio"], float)
        assert data["verbose"] is True
        assert data["tags"] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_invalid_enum_returns_error_not_raise(self):
        """enum 违约：execute 返回错误 ToolResult 而非抛异常"""
        register = make_full_type_register()
        result = await register.execute(
            "full_type_tool", mode="turbo", count=1)
        assert isinstance(result, ToolResult)
        assert result.is_error is True
        assert "错误" in result.content or "不在枚举" in result.content

    @pytest.mark.asyncio
    async def test_missing_required_returns_error(self):
        """缺少必填参数：返回错误 ToolResult"""
        register = make_full_type_register()
        result = await register.execute("full_type_tool", mode="fast")
        assert isinstance(result, ToolResult)
        assert result.is_error is True
        assert "缺少必要参数" in result.content

    @pytest.mark.asyncio
    async def test_range_violation_returns_error(self):
        """数值超出范围：返回错误 ToolResult"""
        register = make_full_type_register()
        result = await register.execute("full_type_tool", mode="fast", count=99)
        assert isinstance(result, ToolResult)
        assert result.is_error is True
        assert "大于 maximum" in result.content or "错误" in result.content

    @pytest.mark.asyncio
    async def test_unknown_parameter_returns_error(self):
        """传入未声明参数：返回错误 ToolResult"""
        register = make_full_type_register()
        result = await register.execute(
            "full_type_tool", mode="fast", count=1, mystery=123)
        assert isinstance(result, ToolResult)
        assert result.is_error is True
        assert "未知参数" in result.content

    @pytest.mark.asyncio
    async def test_nonexistent_tool_returns_error_hint(self):
        """调用未注册工具：返回带可用工具提示的错误 ToolResult"""
        register = make_full_type_register()
        result = await register.execute("no_such_tool", a=1)
        assert isinstance(result, ToolResult)
        assert result.is_error is True
        assert "工具不存在" in result.content
        assert "full_type_tool" in result.content  # hint 中列出可用工具