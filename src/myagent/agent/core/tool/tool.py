from abc import ABC,abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

MIN_CONSECUTIVE_FAILURES = 5  # 熔断阈值下限：防止配置过小让工具被轻易熔断


class ToolErrorType(str, Enum):
    """工具调用错误的分类（观测/评估定位"错在哪一类"用）。

    与 docs/flows/2026-09-11-agent-loop错误处理.md 链路 1 的错误分类矩阵一一对应：
    回喂模型的话术随分类不同，本枚举是该分类在数据层的唯一承载，用于会话与日志
    观测（如统计各类错误的占比、定位高频失败类型）。

    - TOOL_NOT_FOUND   : 工具不存在/未注册，无熔断对象、不计入计数
    - PARSE_ERROR      : 参数不是合法 JSON（普通语法错误）
    - PARSE_TRUNCATED  : 参数 JSON 因 max_tokens 截断而不完整
    - PARSE_NOT_OBJECT : 参数是合法 JSON 但不是对象（如数组）
    - PARAM_VALIDATION : 参数校验失败（缺必填、类型/枚举/范围等不符）
    - TOOL_EXECUTION   : 工具本体执行时抛出异常
    - BREAKER_INTERCEPT: 熔断拦截（execute_intercept 模式入口驳回）
    """

    TOOL_NOT_FOUND = "tool_not_found"
    PARSE_ERROR = "parse_error"
    PARSE_TRUNCATED = "parse_truncated"
    PARSE_NOT_OBJECT = "parse_not_object"
    PARAM_VALIDATION = "param_validation"
    TOOL_EXECUTION = "tool_execution"
    BREAKER_INTERCEPT = "breaker_intercept"


@dataclass
class ToolResult:
    """工具执行结果。

    content 是回传给模型的文本（失败时含错误信息与 hint）；
    error_type 是本次失败的分类，**唯一的状态来源**——None 表示成功，
    非 None 表示失败并指明属于哪一类（供观测/评估定位），失败时必有分类。
    is_error 由 error_type 派生（成功即 False），ToolRegister 依据它做熔断计数
    （失败累加、成功清零）：解析失败与执行失败同等计入，因为熔断防的是
    "模型反复调用一个工具一直出错"，模型侧写坏参数与工具侧执行失败都算。
    duration_ms 由 ToolRegister 在执行边界测量并填充（工具实现不负责），
    成功失败都记，供耗时观测（区分"慢在模型还是慢在工具"）。
    """

    content: str
    error_type: ToolErrorType | None = None
    duration_ms: int | None = None  # 本次工具执行耗时（墙钟毫秒），由 ToolRegister 填充

    @property
    def is_error(self) -> bool:
        """本次执行是否失败（error_type 非空即失败，成功为 False）。"""
        return self.error_type is not None

    @classmethod
    def error(cls, content: str, error_type: ToolErrorType) -> "ToolResult":
        """构造失败结果；必须给出错误分类（观测要求失败必有类型）。"""
        return cls(content=content, error_type=error_type)


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
            要标记失败时返回 ToolResult.error(内容, ToolErrorType.XXX)，
            register 会据 error_type 做熔断计数并记录失败分类。
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
