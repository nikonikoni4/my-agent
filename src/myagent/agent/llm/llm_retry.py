# llm_retry组建订阅agent中的request/error事件，用户判断是否重试

from myagent.agent.execption import (
    LLMConnectionError,
    LLMQuotaError,
    LLMRateLimitError,
)
from myagent.infra.events.payload import RequestErrorPayLoad


class LLMRerty:
    # 重试策略注册表（异常类型 → 重试决策 + 退避参数），调整策略只改这里，不动异常
    # 分类树（见 ADR: 2026-09-09-LLM错误处理分类策略）
    # 退避参数（见 ADR: 2026-09-10-LLM重试延迟退避策略）：retry 与 backoff_retry 共用
    # 一条指数退避公式，差异仅在基准值——retry 基准 0，公式恒为 0，即"立即重试"
    # 参数以普通 dict 传给消费方（loop 侧再承接为退避参数结构），避免 llm 层
    # 反向依赖 core 层
    # 注：工具调用参数 JSON 解析失败已不走异常路径——ToolRegister 以带 hint
    # 的 ToolResult 回喂模型自纠，对应异常类型已从分类树移除
    # 注：dont_retry 档已移除——"不重试但继续下一条"对配置类错误没有意义
    # （见 ADR: 2026-09-15-agent-loop错误处理重构）；未覆盖的错误落到无人认领 →
    # 抛 AgentUnclaimedError 停止本 turn，由用户去改配置
    retry_type = () # 马上重试的类型
    backoff_retry_type = (LLMRateLimitError,LLMQuotaError,LLMConnectionError) # 延迟重试的类型

    # 马上重试档：基准 0，退避恒为 0（等待步被跳过）
    retry_policy = {"base_delay": 0.0, "multiplier": 2.0, "cap": 0.0}
    # 延迟重试档：1s → 2s → 4s → 8s → 16s → 30s（封顶后恒定）
    backoff_policy = {"base_delay": 1.0, "multiplier": 2.0, "cap": 30.0}

    async def request_error_event(self,payload :RequestErrorPayLoad,_next:callable):
        """
        request/error 的 waterfall 订阅方：按 error_type 查重试策略。

        命中返回 {"decision": ..., "policy": {...退避参数...}}（不再往下走）；
        未命中（如来源未细分的 LLMCallError、本 turn 内重试无意义的配置类错误、
        非本模块管辖的错误如工具熔断）await _next() 交给链上下一个订阅方；无人
        认领时 loop 侧抛 AgentUnclaimedError 停止本 turn。

        必须是 async：waterfall 订阅契约要求订阅方可被 await（链上后续订阅方
        可能内部等待外部应答），故委托下游时要 await _next()。契约见
        docs/coding-rules/2026-09-18-waterfall订阅契约.md。

        args:
            payload : RequestErrorPayLoad error_type 为异常实例
            _next   : 链上下一个订阅方；本订阅方不认领时调用
        """
        error = payload.error_type
        if isinstance(error, self.retry_type):
            return {"decision": "retry", "policy": self.retry_policy}
        if isinstance(error, self.backoff_retry_type):
            return {"decision": "backoff_retry", "policy": self.backoff_policy}
        return await _next()
