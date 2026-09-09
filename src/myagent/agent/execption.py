"""项目业务异常定义。

所有业务异常统一继承自 infra 层的 MyAgentError，便于上层用
`except MyAgentError` 做统一兜底；子类用于区分错误来源和决定重试策略。
"""

from myagent.infra.exception import MyAgentError

class ToolValueError(MyAgentError):
    """工具注册/注销时传入非法输入。

    典型场景：tools 为 None 或类型不对、列表中混入非 Tool/非 str 元素、
    工具的 name / description / parameters 为空。
    """

class ToolExecuteError(MyAgentError):
    """工具执行过程中抛出异常时由 ToolRegister.execute 包装抛出。

    __cause__ 保留工具抛出的原始异常，供上层排查。
    """

class ToolValidateParameterError(MyAgentError):
    """模型传入的工具参数未通过 schema 校验。

    典型场景：缺少 required 中声明的字段、参数类型与 schema 声明不符。
    """

class ToolConsecutiveFailureError(MyAgentError):
    """单个工具连续失败达到熔断阈值，且该工具配置了熔断即抛错（人在回路入口）。

    由 ToolRegister.execute 在触发熔断时抛出；经 step 内 TaskGroup 以
    ExceptionGroup 形式上抛，由 loop 接住：记录 request/error 后终止本
    turn（中断当前行为）。与"功能降级"（返回带 hint 的错误结果、agent
    继续运行）相对，本类型表示该工具已不可用、需要外部介入。
    """

class LLMCallError(MyAgentError):
    """LLM 调用失败（来源分类树的基类）。

    由 llm/ 层把 SDK 异常（openai.APIError 家族）翻译而来，__cause__ 保留原始 SDK 异常。
    分类主轴是"错误来源"，子类按来源划分（配置 / 限流配额 / 连接 / 请求内容）。

    —— 这里刻意不把"是否可重试、怎么重试"写进类型 ——
    重试策略是易变信息，走独立的策略注册表（异常类型 → 重试策略）映射，
    这样将来发现某类其实可重试时只改表那行，不动本异常树。
    未登记子类时，则以基类兜底，交给默认策略。
    """

class LLMAuthError(LLMCallError):
    """LLM 认证失败（来源：配置/环境）。

    典型场景：API key 无效 / 无权限 / 过期（openai 401）。
    本质不可重试，重发无意义，应先改配置；是否/如何提示或交上层由策略注册表决定。
    """

class LLMModelError(LLMCallError):
    """LLM 模型/接入点错误（来源：配置/环境）。

    典型场景：模型名或接入点 ID 不存在、写错（openai 404）。
    本质不可重试，应先改模型名/接入点；处理方式由策略注册表决定。
    """

class LLMEndpointError(LLMCallError):
    """LLM 接入地址错误（来源：配置/环境）。

    典型场景：base_url 配错、不可达。
    本质不可重试，应先修正接入地址；处理方式由策略注册表决定。
    """

class LLMRateLimitError(LLMCallError):
    """LLM 限流（来源：限流/配额）。

    典型场景：触发限流（openai 429）。通常延迟后重试（退避）；
    是否重试、退避策略由策略注册表决定。
    """

class LLMQuotaError(LLMCallError):
    """LLM 配额不足（来源：限流/配额）。

    典型场景：账号配额超额/余额用尽。可能需要人工充值后才可继续；
    处理方式由策略注册表决定。
    """

class LLMConnectionError(LLMCallError):
    """LLM 连接/瞬时波动（来源：连接）。

    典型场景：网络断开、上游瞬时不可达。通常延迟后重试；
    是否重试、退避策略由策略注册表决定。
    """

class LLMContextExceededError(LLMCallError):
    """LLM 上下文超长（来源：请求/内容）。

    典型场景：请求 + 历史超过模型上下文窗口。通常需要压缩/降级后重试；
    处理方式由策略注册表决定。
    """

class LLMToolCallParseError(LLMCallError):
    """工具调用的 arguments 参数 JSON 解析失败（非截断）。

    典型场景：模型输出的工具调用 arguments 是非法 JSON（单引号、括号不匹配、
    多余逗号等导致 json.loads 抛 JSONDecodeError），并非 max_tokens 截断所致。

    与 LLMToolCallTruncatedError 的区别：后者特指 finish_reason == 'length'
    截断导致的参数 JSON 不完整；本类型覆盖其余一切解析失败。两者同源于
    "模型工具调用内容不可信"，但重试/恢复策略可能不同，故平铺并列，
    由 agent loop 或策略注册表分别处置。

    由 LLMProvider.parse_tool_call 及流式 tool_calls 拼装处的 json.loads 捕获抛出。
    details 携带 tool_call_id / tool_name / raw_arguments，供上层诊断或补救。
    """

class LLMToolCallTruncatedError(LLMCallError):
    """输出达到 max_tokens 被截断，且截断响应中夹带了参数不完整的工具调用。

    典型场景：finish_reason == 'length' 时响应携带 tool_calls，id/name 完整，
    但 arguments 的 JSON 字符串在生成中途被切断，json.loads 失败
    （实测错误形如 Unterminated string）。流式与非流式均可能出现。

    由 LLMProvider.check_truncated_tool_calls 抛出，由 agent loop 决定恢复策略：
    提高 max_tokens 重试 / 要求模型缩短写入内容 / 人在回路确认。
    details["truncated_tool_calls"] 携带截断工具调用的原始信息
    （id/name/未解析的 arguments 字符串），供上层诊断或补救。
    """
