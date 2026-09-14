from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal
import json
import time

from myagent.agent.core.provider import RawToolCall
from myagent.agent.execption import ToolValueError,ToolValidateParameterError,ToolConsecutiveFailureError
from .tool import Tool, ToolResult, ParsedToolCall, ToolErrorType
import logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class ToolRegister:
    """工具注册表：管理工具的注册、注销、查询、执行与熔断。"""

    def __init__(self, ):
        """初始化一个空的工具注册表。"""
        self._tools :dict[str,Tool]= {}
        # 熔断状态：工具名 -> 本 turn 内当前连续失败次数
        self._consecutive_failures :dict[str,int]= {}
        # 本 turn 内已熔断的工具名；turn/end 事件清空，下一 turn 恢复可用
        self._tripped :set[str]= set()

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
        maxItems / minimum / maximum / exclusiveMinimum / exclusiveMaximum /
        multipleOf / uniqueItems 等关键字校验，未设置对应关键字即无该限制。

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
            exclusive_minimum = schema_value.get("exclusiveMinimum")
            exclusive_maximum = schema_value.get("exclusiveMaximum")
            multiple_of = schema_value.get("multipleOf")
            # 闭区间：大于等于 / 小于等于
            if minimum is not None and normalized < minimum:
                raise ToolValidateParameterError(f"数值 {normalized} 小于 minimum={minimum}")
            if maximum is not None and normalized > maximum:
                raise ToolValidateParameterError(f"数值 {normalized} 大于 maximum={maximum}")
            # 开区间：严格大于 / 严格小于
            if exclusive_minimum is not None and normalized <= exclusive_minimum:
                raise ToolValidateParameterError(f"数值 {normalized} 未 > exclusiveMinimum={exclusive_minimum}")
            if exclusive_maximum is not None and normalized >= exclusive_maximum:
                raise ToolValidateParameterError(f"数值 {normalized} 未 < exclusiveMaximum={exclusive_maximum}")
            # 倍数：必须是 multipleOf 的整数倍（浮点误差用余数容差规避）
            if multiple_of is not None:
                quotient = normalized / multiple_of
                if abs(round(quotient) - quotient) > 1e-9:
                    raise ToolValidateParameterError(f"数值 {normalized} 不是 {multiple_of} 的整数倍")

        if schema_type == "array" and isinstance(normalized, list):
            min_items = schema_value.get("minItems")
            max_items = schema_value.get("maxItems")
            if min_items is not None and len(normalized) < min_items:
                raise ToolValidateParameterError(f"数组长度 {len(normalized)} 小于 minItems={min_items}")
            if max_items is not None and len(normalized) > max_items:
                raise ToolValidateParameterError(f"数组长度 {len(normalized)} 大于 maxItems={max_items}")
            # 唯一性：要求元素互不相同
            if schema_value.get("uniqueItems") is True and len(set(normalized)) != len(normalized):
                raise ToolValidateParameterError(f"数组 {normalized} 存在重复元素，uniqueItems 要求互不相同")

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

    def _validate(self, parameter_schemas: dict, parameters: dict)->dict:
        """参数校验的统一入口，组合各项校验步骤。

        Args:
            parameter_schemas: 工具的参数 schema（即 Tool.parameters 返回的 dict）。
            parameters: 模型实际传入的参数键值对。

        Raises:
            ToolValidateParameterError: 任一校验步骤（类型、必填参数）不通过时抛出。
        """
        # 验证required 参数是否都包含
        self._validate_required_parameters(parameter_schemas,parameters)
        return self._validate_param(parameter_schemas,parameters)

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

    def parse_call(self, call: RawToolCall) -> ParsedToolCall | ToolResult:
        """把模型原始工具调用解析为可执行形态（工具层的信任边界入口）。

        模型输出的 arguments JSON 字符串不可信，json.loads 在此完成：
        - 解析成功且是 dict：返回 ParsedToolCall，交由 execute 继续校验执行
        - 解析失败或不是 dict：返回带 hint 的错误 ToolResult（原文 + 错误
          位置），回喂模型自纠；工具未执行

        解析失败的话术依据 call.truncated 分流（见 ADR 截断三成因）：
        截断（来自 finish_reason=length 的响应）建议精简参数/拆分调用，
        普通语法错误建议对照错误位置修正。

        Args:
            call: 模型发起的原始工具调用

        Returns:
            ParsedToolCall（解析成功）或 ToolResult（error_type 为 PARSE_*
            之一，解析失败）
        """
        raw = call.arguments or "{}"
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError as e:
            if call.truncated:
                hint = (
                    "\n hint : 本次回复因达到 max_tokens 被截断，上述参数大概率"
                    "是在生成中途被切断（不完整）；请精简参数内容后重新调用，"
                    "必要时拆分为多次调用"
                )
            else:
                hint = (
                    "\n hint : 上述原文是你本次工具调用的 arguments，存在 JSON "
                    "语法错误；请对照错误位置修正后重新调用该工具"
                )
            return ToolResult(
                content=(
                    f"status : error\n message : 工具调用({call.name})参数不是合法 JSON："
                    f"{e.msg}（位置 {e.pos}）\n raw_arguments : {raw}" + hint
                ),
                error_type=(
                    ToolErrorType.PARSE_TRUNCATED if call.truncated
                    else ToolErrorType.PARSE_ERROR
                ),
            )
        if not isinstance(arguments, dict):
            return ToolResult(
                content=(
                    f"status : error\n message : 工具调用({call.name})参数解析结果不是"
                    f"JSON 对象（得到 {type(arguments).__name__}）\n raw_arguments : {raw}"
                    "\n hint : arguments 必须是 {...} 形式的 JSON 对象；请重新调用该工具"
                ),
                error_type=ToolErrorType.PARSE_NOT_OBJECT,
            )
        return ParsedToolCall(call_id=call.id, tool_name=call.name, arguments=arguments)

    async def execute(self, call: RawToolCall) -> ToolResult:
        """执行一次模型发起的工具调用：解析参数 → 校验 → 执行，并记录执行耗时。

        Args:
            call: 模型发起的原始工具调用，arguments 为 wire 上的 JSON 字符串

        Returns:
            工具执行结果。参数 JSON 解析失败、参数校验失败、执行抛异常时
            返回 is_error=True 的 ToolResult（错误文本 + hint），供模型自纠；
            工具不存在返回错误结果但无熔断对象、不计入计数。

        Raises:
            ToolConsecutiveFailureError: 工具触发熔断且配置了 raise_on_break
                （人在回路入口），上抛由 loop 接住中断本 turn。
        """
        # 计时口径：整个 execute（解析→校验→执行），即"一次工具调用换回结果的真实等待"；
        # 成功与失败都记。熔断抛错路径不返回结果，故不填耗时（无结果可承载）。
        started = time.perf_counter()
        result = await self._execute(call)
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result

    async def _execute(self, call: RawToolCall) -> ToolResult:
        """解析→校验→执行的实现（耗时由 execute 包裹，本方法不关心）。"""
        # 信任边界：先解析模型给的原始 arguments（纯解析，无副作用）
        parsed = self.parse_call(call)

        tool_name = call.name
        if tool_name not in self._tools:
            logger.warning(f"{tool_name}工具不存在/未注册")
            return ToolResult.error(
                f"status : error\n message : {tool_name}工具不存在\n hint : 可用工具 {','.join(self.tool_list())}",
                ToolErrorType.TOOL_NOT_FOUND,
            )
        tool = self._tools[tool_name]

        # 熔断拦截：仅 execute_intercept 模式在入口驳回。schema_hide 模式由
        # to_schemas 过滤，模型依据对话历史硬调已熔断工具时仍会执行到这里
        # （缺口记录在 架构设计/工具调用.md 已知限制 4）
        if tool.breaker_mode == "execute_intercept" and tool_name in self._tripped:
            return ToolResult.error(
                f"status : error\n message : 工具{tool_name}已因连续失败{self._consecutive_failures.get(tool_name, 0)}次被熔断，本轮内不可再调用"
                f"\n hint : 请改用其他工具完成任务，或告知用户当前工具不可用",
                ToolErrorType.BREAKER_INTERCEPT,
            )

        # 解析失败：模型输出问题，但与执行失败同等计入熔断——熔断防的是
        # "模型反复调用一个工具一直出错"，模型侧写坏参数与工具侧执行失败
        # 都算，防止坏参数调用无限循环
        if isinstance(parsed, ToolResult):
            result = parsed
        else:
            # 工具参数校验 + 执行
            try:
                kwargs = self._validate(tool.parameters,parsed.arguments)
                raw = await tool.execute(**kwargs)
                # 子类返回 str 视为成功；返回 ToolResult 按其 is_error 判定
                result = raw if isinstance(raw, ToolResult) else ToolResult(content=str(raw))
            except ToolValidateParameterError as e:
                logger.error(f"{tool_name}工具调用错误，参数:{parsed.arguments},错误信息：{e}")
                result = ToolResult.error(
                    f"status : error\n message : {tool_name}工具调用参数未通过校验：{e}\n 参数 : {parsed.arguments}"
                    f"\n hint : 请分析上述调用错误信息，修正参数后重新调用",
                    ToolErrorType.PARAM_VALIDATION,
                )
            except Exception as e:
                # 暂时的写法，这里工具调用错误还需要分类进行
                logger.error(f"{tool_name}工具调用错误，参数:{parsed.arguments},错误信息{e}")
                result = ToolResult.error(
                    f"status : error\n message : {tool_name}工具调用过程中出现异常，属于工具内部错误\n 参数 : {parsed.arguments}"
                    f"\n hint : 重试后若仍不可行，建议放弃使用该工具",
                    ToolErrorType.TOOL_EXECUTION,
                )

        # 熔断计数：解析/校验/执行的失败都算一次，成功清零
        if result.is_error:
            self._on_failure(tool, result)
        else:
            self._consecutive_failures[tool_name] = 0
        return result

    def _on_failure(self, tool: Tool, result: ToolResult) -> None:
        """记录一次失败并处理熔断触发（计数达到阈值时抛错或附 hint）。"""
        if tool.max_consecutive_failures is None:
            return  # 未配置熔断，由 agent 的 step_limit 兜底
        count = self._consecutive_failures.get(tool.name, 0) + 1
        self._consecutive_failures[tool.name] = count
        if count < tool.max_consecutive_failures:
            return
        self._tripped.add(tool.name)
        if tool.raise_on_break:
            # 抛错上抛，由 loop 识别后记入 step_error 并触发 request/error；
            # 人在回路的控制由 request/error 的 waterfall 订阅方给出，后续接入
            raise ToolConsecutiveFailureError(
                f"工具{tool.name}连续失败{count}次触发熔断，该工具配置熔断即抛错，中断当前行为"
            )
        # 功能降级：触发熔断的当次结果附 hint。schema_hide 模式下模型下次
        # 看不到该工具的 schema，这条 hint 是它获知熔断、换路的唯一渠道
        result.content = (
            f"{result.content}\n hint : 工具{tool.name}连续失败{count}次已熔断，本轮内不可再用，"
            f"请改用其他工具完成任务，或告知用户当前工具不可用、无法完成任务"
        )

    def reset_breaker(self, payload=None) -> None:
        """清空熔断状态（连续失败计数与已熔断集合），下一 turn 工具恢复可用。

        作为 turn/end 事件回调由 loop 接线订阅；也可直接调用。
        """
        self._consecutive_failures.clear()
        self._tripped.clear()

    def to_schemas(self)->list[dict]:
        """编译所有已注册工具的 schema（含熔断过滤）。

        schema_hide 模式的已熔断工具不出现在结果里，模型下次请求起不再
        看到该工具（注意：schema 变化会使本次请求的缓存命中失效）。

        Returns:
            每个未熔断（或非 schema_hide 模式）工具 to_schema() 结果组成的
            列表，注册表为空时返回空列表。
        """
        schemas = []
        for tool in self._tools.values():
            if tool.breaker_mode == "schema_hide" and tool.name in self._tripped:
                continue
            schemas.append(tool.to_schema())
        return schemas