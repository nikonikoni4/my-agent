"""agent 隔离容器的对外导出。

AgentContext：一个 agent 的完整组件持有者——独立总线、Session、SystemPrompt、
策略组件与 ReActAgentLoop。隔离由"每个 ctx 各持一份 EventService"保证。
AgentPolicySpec：策略组件的注册描述，登记事件、绑定方法与对象本体，供
register_policy 做校验并保活。
"""

from myagent.agent.agent_context.agent_context import AgentContext, AgentPolicySpec

__all__ = [
    "AgentContext",
    "AgentPolicySpec",
]
