from abc import ABC,abstractmethod
from typing import Any

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
        工具描述,需要描述：
        0. 作用是什么
        1. 什么时候调用，什么时候不调用
        2. 返回什么内容
        """
        pass
    @property
    @abstractmethod
    def parameters(self)-> dict[str, Any]:
        """
        返回工具参数的 JSON Schema（首层固定为 object）。格式约定与示例：
        {
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

                以下是可校验的约束关键字（enum 的同级关键字），未设置某关键字即表示无该项限制：

                example4: 字符串：枚举 + 长度区间
                "parameter_name" : {
                    "type" : "string",
                    "description" : "...",
                    "enum" : ["auto","manual"],   ← 限定取值，可选
                    "minLength" : 2,              ← 最小长度，可选
                    "maxLength" : 20,             ← 最大长度，可选
                }

                example5: 字符串：正则匹配 pattern
                "parameter_name" : {
                    "type" : "string",
                    "description" : "...",
                    "pattern" : "^[0-9]{11}$",     ← 必须匹配的正则，可选
                }

                example6: 整数/数字：数值范围（number 同样适用）
                "parameter_name" : {
                    "type" : "integer",
                    "description" : "...",
                    "minimum" : 1,                ← 下限（闭区间），可选
                    "maximum" : 100,              ← 上限（闭区间），可选
                }
                # 需要开区间用 exclusiveMinimum / exclusiveMaximum：
                #   "exclusiveMinimum" : 0  表示 > 0
                #   "exclusiveMaximum" : 10 表示 < 10
                # 需要限制为某个数的倍数用 multipleOf：
                #   "multipleOf" : 5  表示必须是 5 的倍数

                example7: 整数/数字：开区间 + 倍数
                "parameter_name" : {
                    "type" : "number",
                    "description" : "...",
                    "exclusiveMinimum" : 0,       ← > 0，可选
                    "exclusiveMaximum" : 1,       ← < 1，可选
                    "multipleOf" : 0.1,           ← 必须是 0.1 的倍数，可选
                }

                example8: 数组：长度与唯一性
                "parameter_name" : {
                    "type" : "array",
                    "description" : "...",
                    "items" : { "type" : "string" },
                    "minItems" : 1,               ← 最少元素数，可选
                    "maxItems" : 10,              ← 最多元素数，可选
                    "uniqueItems" : true,         ← 是否要求元素唯一，可选
                }

                example9: 对象：是否允许多余字段
                "parameter_name" : {
                    "type" : "object",
                    "description" : "...",
                    "properties" : { },
                    "additionalProperties" : false,  ← false 则拒绝未声明字段，可选
                }

                example10: 无参数工具
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