from abc import ABC,abstractmethod
import json
from typing import Any
from myagent.agent.execption import ToolValueError,ToolExecuteError,ToolValidateParameterError
import logging 
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
class Tool(ABC):
    """工具抽象基类。

    子类实现 name / description / parameters / execute 四个成员，
    即可被 ToolRegister 注册，并通过 to_schema() 编译为 OpenAI 工具 schema。
    """

    def __init__(self):
        pass

    @property
    @abstractmethod
    def name(self)->str:
        """
        工具名称
        """
        pass
    @property
    @abstractmethod
    def description(self)->str:
        """
        工具描述
        """
        pass
    @property
    @abstractmethod
    def parameters(self)-> dict[str, Any]:
        """
        返回工具参数的 JSON Schema（首层固定为 object）。格式约定与示例：
            "type" : "object" , <- 第一层嵌套固定是object
            "properties" : {
                "<parameter_name>" : {
                    "type" : string / number / integer / boolean / array / object
                    "description": "...",     ← 可有，给模型的说明
                    "items": {...},           ← type 是 array 时才有
                    "enum": ["a", "b"],       ← type 是 string/integer 时可选，限定取值
                
                # example1: 单个string/integer等参数
                "parameter_name" : { 
                    "type" : "string",
                    "description" : "...",
                    "enum" : ["a","b"],
                }
                # example2:多个参数
                "parameter_name" :  {
                    "type": "object",
                    "description" : "...",
                    "properties" : {
                    }
                }

                example3: 输入字符串数组等
                "parameter_name" : {
                    "type" : "array",
                    "description" : "...",
                    "items" : {
                        "type": "string"
                    }
                }

                example4: 无参数工具
                <parameters整个直接用空object，properties和required为空>
                {
                    "type" : "object",
                    "properties" : {},
                    "required" : []
                }
            }
            "required":[...]
        }
        """
        pass

    @abstractmethod
    async def execute(self,)->str:
        """执行工具的具体逻辑。

        Args:
            **kwargs: 模型 arguments 经 json.loads 后的键值对，
                键与 parameters schema 中 properties 声明对应。

        Returns:
            执行结果字符串，会作为 tool 消息的 content 回传给模型。
        """
        pass

    def validate(self):
        """参数校验"""
        pass

    def to_schema(self)->dict[str,Any]:
        """编译为 OpenAI tools 顶层的单个 function schema。

        Returns:
            形如 {"type": "function", "function": {name, description, parameters}} 的 dict。
        """
        return {
            "type" : "function",
            "function":{
                "name" : self.name,
                "description":self.description,
                "parameters":self.parameters
            }
        }



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
    def _validate_type(self, parameter_schemas: dict, parameters: dict)->dict:
        """校验模型传入参数的类型是否与 schema 声明一致。

        Args:
            parameter_schemas: 工具的参数 schema（即 Tool.parameters 返回的 dict）。
            parameters: 模型实际传入的参数键值对。

        Raises:
            ToolValidateParameterError: 参数的实际类型与 schema 声明的 type 不符时抛出。
        """
        parameter_schemas:dict = parameter_schemas.get("properties",None)
        if not parameter_schemas:
            # 无参数，不需要验证
            return

        for parameter_name,parameter_value in parameters.items():
            schema_value = parameter_schemas.get(parameter_name,None)
            if schema_value: # string / number / integer / boolean / array / object
                if schema_value == "boolean" :
                    # if isinstance(parameter_value,str)
                    pass 
            else:
                raise ToolValidateParameterError(f"tool_call输入未知参数{parameter_name}")
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