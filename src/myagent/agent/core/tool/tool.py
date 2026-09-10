from abc import ABC,abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

MIN_CONSECUTIVE_FAILURES = 5  # 熔断阈值下限：防止配置过小让工具被轻易熔断


@dataclass
class ToolResult:
    """工具执行结果。

    content 是回传给模型的文本（解析/校验/执行失败时含错误信息与 hint）；
    is_error 标记本次执行是否失败，ToolRegister 依据它做熔断计数（失败累加、
    成功清零）。is_parse_error 标记失败发生在"模型参数 JSON 解析"这一步
    （工具未执行）：是模型输出问题而非工具问题，但与执行失败同等计入
    熔断——熔断防的是"模型反复调用一个工具一直出错"，模型侧写坏参数
    与工具侧执行失败都算。
    """

    content: str
    is_error: bool = False
    is_parse_error: bool = False

    @classmethod
    def error(cls, content: str) -> "ToolResult":
        """构造失败结果（is_error=True）。"""
        return cls(content=content, is_error=True)


@dataclass
class ParsedToolCall:
    """RawToolCall 解析后的可执行形态（工具层内部使用）。

    arguments 已通过 json.loads 从 wire 字符串解析为 dict；解析失败的调用
    不会产生本类型——由 ToolRegister 直接以带 hint 的错误 ToolResult
    回喂模型自纠。
    """

    call_id: str
    tool_name: str
    arguments: dict[str, Any]

class Tool(ABC):
    """工具抽象基类。

    子类实现 name / description / parameters / execute 四个成员，
    即可被 ToolRegister 注册，并通过 to_schema() 编译为 OpenAI 工具 schema。
    """

    def __init__(
        self,
        max_consecutive_failures: int | None = None,
        breaker_mode: Literal["schema_hide", "execute_intercept"] = "schema_hide",
        raise_on_break: bool = False,
    ):
        """熔断配置（语义见 架构设计/工具调用.md）。

        Args:
            max_consecutive_failures: 熔断阈值，本 turn 内连续失败达到该次数即
                熔断。None 表示不熔断，由 agent 的 step_limit 兜底防死循环；
                设置时必须 >= MIN_CONSECUTIVE_FAILURES（下限 5）。
            breaker_mode: 熔断方式，二选一。schema_hide：下次请求起从 tools
                schema 中移除（默认，最稳，代价是缓存命中失效）；execute_intercept：
                schema 保留，execute 入口驳回调用。
            raise_on_break: 熔断时是否抛 ToolConsecutiveFailureError（人在回路
                入口，由 loop 接住并中断本 turn）；False 时功能降级，触发熔断的
                当次结果附 hint，agent 继续运行。
        """
        if max_consecutive_failures is not None and max_consecutive_failures < MIN_CONSECUTIVE_FAILURES:
            raise ValueError(
                f"max_consecutive_failures={max_consecutive_failures} 低于下限 "
                f"{MIN_CONSECUTIVE_FAILURES}；不熔断请传 None（由 agent 的 step_limit 兜底）"
            )
        self.max_consecutive_failures = max_consecutive_failures
        self.breaker_mode = breaker_mode
        self.raise_on_break = raise_on_break

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
    async def execute(self,)->ToolResult | str:
        """执行工具的具体逻辑。

        Args:
            **kwargs: 模型 arguments 经 json.loads 后的键值对，
                键与 parameters schema 中 properties 声明对应。

        Returns:
            返回 str 视为成功（由 register 包装为 ToolResult(content=...)）；
            要标记失败时返回 ToolResult.error(...)，register 会据此做熔断计数。
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
