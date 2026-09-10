# llm_retry组建订阅agent中的request/error事件，用户判断是否重试

from myagent.agent.execption import (
    LLMAuthError,
    LLMConnectionError,
    LLMContextExceededError,
    LLMEndpointError,
    LLMModelError,
    LLMQuotaError,
    LLMRateLimitError,
)
from myagent.infra.events.payload import RequestErrorPayLoad

class LLMRerty:
    # 重试策略注册表（异常类型 → 重试决策），调整策略只改这里，不动异常
    # 分类树（见 ADR: 2026-09-09-LLM错误处理分类策略）
    # 注：工具调用参数 JSON 解析失败已不走异常路径——ToolRegister 以带 hint
    # 的 ToolResult 回喂模型自纠，对应异常类型已从分类树移除
    retry_type = () # 马上重试的类型
    backoff_retry_type = (LLMRateLimitError,LLMQuotaError,LLMConnectionError) # 延迟重试的类型
    dont_retry_type = (LLMAuthError,LLMModelError,LLMEndpointError,LLMContextExceededError) # 不重试的类型

    def request_error_event(self,payload :RequestErrorPayLoad,_next:callable):
        """
        request/error 的 waterfall 订阅方：按 error_type 查重试策略。

        命中返回 {"decision": ...}；未命中（如来源未细分的 LLMCallError、
        非本模块管辖的错误如工具熔断）调用 _next() 交给链上下一个订阅方。

        args:
            payload : RequestErrorPayLoad error_type 为异常实例
        """
        error = payload.error_type
        if isinstance(error, self.retry_type):
            return {"decision": "retry"}
        if isinstance(error, self.backoff_retry_type):
            return {"decision": "backoff_retry"}
        if isinstance(error, self.dont_retry_type):
            return {"decision": "dont_retry"}
        return _next()
