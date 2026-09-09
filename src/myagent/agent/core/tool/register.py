from copy import deepcopy
from typing import Any
from myagent.agent.execption import ToolValueError,ToolExecuteError,ToolValidateParameterError
from .tool import Tool
import logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class ToolRegister:
    """工具注册表：管理工具的注册、注销、查询与执行。"""

    def __init__(self, ):
        """初始化一个空的工具注册表。"""
        self._tools :dict[str,Tool]= {}

    def _validate_required_parameters(self, parameter_schemas: dict, parameters: dict):
        """校验模型传入的参数是否覆盖 schema 中声明的必填字段。

        Args:
            parameter_schemas: 工具的参数 schema（即 Tool.parameters 返回的 dict），
                必填字段列表取自其 "required" 键。
            parameters: 模型实际传入的参数键值对（arguments 经 json.loads 后的 dict）。

        Raises:
            ToolValidateParameterError: 缺少 required 中声明的参数时抛出，
                message 中携带缺失的字段名。
        """
        required_parameters = parameter_schemas.get("required", None)
        if not required_parameters:
            return
        for required in required_parameters:
            if required not in parameters:
                raise ToolValidateParameterError(f"缺少必要参数{required}")

    def _validate_param_value(self, schema_value: dict, parameter_value: Any) -> Any:
        """对单个参数做类型归一化与值校验，返回处理后的新值（不修改入参）。

        类型归一化：模型可能以字符串形式传入非字符串参数，这里按 schema 声明的
        type 尽力转回对应类型（string / array / object 不做转换）：
            boolean : "true"/"True"/"1" -> True，其他字符串 -> False
            integer : "42"              -> 42
            number  : "3.14"/"42"       -> float
        值校验：依据 schema 中的 enum / minLength / maxLength / minItems /
        maxItems / minimum / maximum / exclusiveMinimum / exclusiveMaximum 等
        关键字校验，未设置对应关键字即无该限制。

        Args:
            schema_value: 单个参数的 schema（即 properties 里该字段的 dict）。
            parameter_value: 模型实际传入该参数的原始值。

        Returns:
            归一化并校验通过后的新值。

        Raises:
            ToolValidateParameterError: 值违反 enum / 数值范围 / 长度等约束时抛出。
        """
        if not isinstance(schema_value, dict):
            return parameter_value

        schema_type = schema_value.get("type", "")
        normalized = parameter_value

        # ---- 类型归一化 ----
        if schema_type == "boolean":
            if isinstance(parameter_value, str):
                normalized = parameter_value in {"true", "True", "1"}
        elif schema_type == "integer":
            if isinstance(parameter_value, str):
                normalized = int(parameter_value)  # 非数字字符串会抛 ValueError
        elif schema_type == "number":
            if isinstance(parameter_value, str):
                normalized = float(parameter_value)  # 非数字字符串会抛 ValueError

        # ---- 值校验 ----
        enum = schema_value.get("enum")
        if enum is not None and normalized not in enum:
            raise ToolValidateParameterError(f"参数取值 {normalized!r} 不在枚举 {enum} 内")

        if schema_type == "string" and isinstance(normalized, str):
            min_len = schema_value.get("minLength")
            max_len = schema_value.get("maxLength")
            if min_len is not None and len(normalized) < min_len:
                raise ToolValidateParameterError(f"字符串长度 {len(normalized)} 小于 minLength={min_len}")
            if max_len is not None and len(normalized) > max_len:
                raise ToolValidateParameterError(f"字符串长度 {len(normalized)} 大于 maxLength={max_len}")

        if schema_type in ("integer", "number") and isinstance(normalized, (int, float)) and not isinstance(normalized, bool):
            minimum = schema_value.get("minimum")
            maximum = schema_value.get("maximum")
            if minimum is not None and normalized < minimum:
                raise ToolValidateParameterError(f"数值 {normalized} 小于 minimum={minimum}")
            if maximum is not None and normalized > maximum:
                raise ToolValidateParameterError(f"数值 {normalized} 大于 maximum={maximum}")

        if schema_type == "array" and isinstance(normalized, list):
            min_items = schema_value.get("minItems")
            max_items = schema_value.get("maxItems")
            if min_items is not None and len(normalized) < min_items:
                raise ToolValidateParameterError(f"数组长度 {len(normalized)} 小于 minItems={min_items}")
            if max_items is not None and len(normalized) > max_items:
                raise ToolValidateParameterError(f"数组长度 {len(normalized)} 大于 maxItems={max_items}")

        return normalized


    def _validate_param(self, parameter_schemas: dict, parameters: dict)->dict:
        """校验并归一化模型传入的整组参数。

        逐个参数交给 _validate_param_value 处理（含类型归一化与值校验），
        返回归一化后的新 dict（深拷贝，不改动入参）；未知参数直接抛错。

        Args:
            parameter_schemas: 工具的参数 schema（即 Tool.parameters 返回的 dict）。
            parameters: 模型实际传入的参数键值对。

        Returns:
            归一化后的参数 dict。

        Raises:
            ToolValidateParameterError: 参数名不在 schema.properties 中声明时抛出。
        """
        properties = parameter_schemas.get("properties", None)
        if not properties:
            # 无参数，不需要验证
            return parameters
        try :
            vp :dict=deepcopy(parameters) # 归一化后的参数
            for parameter_name,parameter_value in parameters.items():
                schema_value:dict = properties.get(parameter_name,None)
                if not schema_value:
                    raise ToolValidateParameterError(f"tool_call输入未知参数{parameter_name}")
                vp[parameter_name] = self._validate_param_value(schema_value, parameter_value)
        except (ValueError, TypeError) as e:
            raise ToolValidateParameterError(f"验证参数{parameter_name}时发生错误，真实值为{parameter_value!r}") from e
        return vp

    def validate(self, parameter_schemas: dict, parameters: dict):
        """参数校验的统一入口，组合各项校验步骤。

        Args:
            parameter_schemas: 工具的参数 schema（即 Tool.parameters 返回的 dict）。
            parameters: 模型实际传入的参数键值对。

        Raises:
            ToolValidateParameterError: 任一校验步骤（类型、必填参数）不通过时抛出。
        """
        # 验证required 参数是否都包含
        self._validate_required_parameters(parameter_schemas,parameters)
        # 验证参数类型是否正确
        # pass 暂时先不做
        # 验证参数范围是否在默认范围内
        # pass 这一个暂时不做
        pass

    def register(self, tools: Tool | list[Tool]):
        """注册一个或多个工具。

        同名工具重复注册时跳过并记录 warning，不做覆盖。

        Args:
            tools: 待注册的单个 Tool 实例，或 Tool 实例列表。

        Raises:
            ToolValueError: tools 为 None、类型不是 Tool 或 list、
                列表中存在非 Tool 元素，或工具的 name / description / parameters 为空。
        """
        # 检查输入
        if tools is None or not (isinstance(tools, Tool) or isinstance(tools, list)):
            raise ToolValueError("注册工具为None 或 非tools类型")
        if isinstance(tools, Tool):
            tools = [tools]

        # 注册工具
        for tool in tools:
            if not isinstance(tool, Tool):
                raise ToolValueError(f"列表中存在非 Tool 元素: {type(tool)}")
            if not tool.description or not tool.name or not tool.parameters:
                raise ToolValueError("工具描述或名称或参数为空")
            if tool.name in self._tools :
                logger.warning(f"工具{tool.name}重复注册或有同名tool")
                continue
            self._tools[tool.name] = tool

    def unregister(self, tools: str | list[str]):
        """注销一个或多个已注册的工具。

        按工具名注销；注销不存在的工具名时静默跳过，不报错。

        Args:
            tools: 待注销的单个工具名，或工具名列表。

        Raises:
            ToolValueError: tools 为 None、类型不是 str 或 list、
                或列表中存在非 str 元素。
        """
        # 检查输入
        if tools is None or not (isinstance(tools, str) or isinstance(tools, list)):
            raise ToolValueError(f"注销输入参数错误{type(tools)},正确应该是str | list(str)")
        if isinstance(tools,str):
            tools = [tools]

        for tool in tools:
            if not isinstance(tool, str):
                raise ToolValueError(f"列表中存在非 str 元素: {type(tool)}")
            self._tools.pop(tool,None)

    def tool_list(self) -> list[str]:
        """获取所有已注册工具的名字。

        Returns:
            已注册工具名列表，注册表为空时返回空列表。
        """
        return list(self._tools.keys())

    async def execute(self, tool_name, **kwargs) -> str:
        """按名称执行工具。

        Args:
            tool_name: 工具名，即注册时 Tool.name 的值。
            **kwargs: 传给工具 execute 的参数（模型 arguments 解析后的键值对）。

        Returns:
            工具的执行结果。工具不存在时不抛异常，返回带 status/message/hint
            的错误 JSON 字符串，供模型自行纠正。

        Raises:
            ToolExecuteError: 工具执行过程中抛出任意异常时，记录日志后包装抛出。
        """
        if tool_name not in self._tools:
            logger.warning(f"{tool_name}工具不存在/未注册")
            return f"status : error \n message : {tool_name}工具不存在 \n hint : 可用工具 {','.join(self.tool_list())} "
        # 工具参数校验
        # pass

        try:
            return await self._tools[tool_name].execute(**kwargs)
        except ToolValidateParameterError as e:
            logger.error(f"{tool_name}工具调用错误，参数:{kwargs},错误信息：{e}")
            return f"{tool_name}工具调用错误，参数:{kwargs},错误信息：{e}"
        except Exception as e:
            # 暂时的写法，这里工具调用错误还需要分类进行
            logger.error(f"{tool_name}工具调用错误，参数:{kwargs}")
            return f"{tool_name}工具调用错误，参数:{kwargs}"

    def to_schemas(self)->list[dict]:
        """编译所有已注册工具的 schema。

        Returns:
            每个已注册工具 to_schema() 结果组成的列表，注册表为空时返回空列表。
        """
        schemas = []
        for tool in self._tools.values():
            schemas.append(tool.to_schema())
        return schemas