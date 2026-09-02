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

class LLMCallError(MyAgentError):
    """LLM 调用失败。

    由 llm/ 层把 SDK 异常（openai.APIError 家族）翻译而来，
    __cause__ 保留原始 SDK 异常。
    """
