import logging
from collections.abc import Callable
from dataclasses import dataclass

from myagent.agent.core.agent.loop import ReActAgentLoop
from myagent.agent.core.agent.types import AgentConfig
from myagent.agent.core.session import Session
from myagent.agent.core.systemprompt import SystemPrompt
from myagent.infra.events import EventService
from myagent.infra.events.eventspec import EventSpec

logger = logging.getLogger(__name__)


@dataclass
class AgentPolicySpec[T]:
    """
    一个策略组件 如LLMRetry等，只能注册一次，可以注册内部多个策略
    """
    eventspec : EventSpec # 策略注册的事件
    callbacks : list[Callable]
    object : T

    def __post_init__(self):
        if len(self.callbacks) == 0:
            raise ValueError("传入的callback为空")
        for callback in self.callbacks:
            if not callable(callback) or getattr(callback, "__self__", None) is not self.object:
                raise ValueError("callback 必须是该策略对象的绑定方法")



class AgentContext:
    """一个 agent 实例的组件持有者：独立总线 + 全套归属组件。

    隔离落点是"谁持有 EventService"——每个 ctx 在构造时各建一份总线，agent 内的
    事件都在这一份上注册和派发，因此同名事件不会触达其他 agent（见 ADR
    docs/adr/2026-10-02-AgentContext组件归属与事件隔离.md 决策 1）。

    组件依赖保持单向：loop / session 接收的是 EventService 等直接依赖，不反向依赖
    ctx；ctx 只负责归属与生命周期，不挡在组件之间。

    注意：构造即完成装配，包括 session 的显式 bind。传入的 session 必须尚未绑定，
    否则 bind 抛 SessionReBindError。持久化组件由 bind 在有文件路径时创建，因此
    文件态会话的构造需要在运行中的事件循环内进行。
    """

    def __init__(
        self,
        agent_name : str,
        session : Session,
        agent_config : AgentConfig,
        llm_client,
        prompt_render_parame : dict|None,
    ):
        """
        Args:
            agent_name: agent 语义名，需要"语义名称-uuid"形式作为唯一标识；
                本类不校验格式，原样透传给 loop 参与提示词装配。
            session: 本 agent 的会话，由调用方构造（见 SessionStore.create/load），
                必须尚未绑定——绑定由本构造完成。
            agent_config: loop 运行参数（step_limit、max_retry_count）。
            llm_client: 模型提供方，直接透传给 loop。
            prompt_render_parame: 提示词渲染参数，透传给 loop。这是一个待优化的参数，
                本身不应该作为 AgentContext 的入参。
        """
        self.agent_name = agent_name
        self.event_service = EventService()
        self.system_prompt = SystemPrompt()
        self.session = session
        # 运行装配点：先把本 agent 的总线绑给 session，绑定后 append 才广播并落盘
        self.session.bind(self.event_service)
        self.policy_objects : list = []
        self.agent_loop = ReActAgentLoop(
            self.event_service,
            self.session,
            self.system_prompt,
            agent_config,
            llm_client,
            self.agent_name,
            prompt_render_parame
            )

    def register_policy(self,policies:list[AgentPolicySpec]):
        """把策略组件注册到本 agent 的总线上，并持强引用防止被弱引用回收。

        Args:
            policies: 一个或多个 AgentPolicySpec；传单个 spec 也接受，内部包成列表。

        Returns:
            None。

        Events:
            按 spec.eventspec 逐条注册 spec.callbacks；不主动派发。

        Raises:
            无。已注册过的对象记一条 warning 后跳过，不重复注册。
        """
        if not isinstance(policies,list):
            policies = [policies]
        for policy in policies:
            if any(obj is policy.object for obj in self.policy_objects):
                logger.warning(f"{type(policy.object).__name__} 已经注册过")
                continue
            self.policy_objects.append(policy.object)
            for callback in policy.callbacks:
                self.event_service.register(policy.eventspec.name,callback)


    async def close(self) -> None:
        """按 ADR 的关闭顺序收尾：先停 loop 并等它结束，再停持久化。

        顺序不可颠倒——loop 收尾期间仍可能向 session 追加记录，先停持久化会丢掉这段。

        Args:
            无。

        Returns:
            None。

        Events:
            间接：loop 取消时其内部触发 step/turn 的 interrupted 终态事件。

        Raises:
            无。loop 任务被取消属预期，不再上抛；调用方自身的取消原样传播。
        """
        # 步骤 1：请求停止执行，并等待后台循环真正结束
        await self.agent_loop.stop()
        # 步骤 2：停止持久化——等后台写任务结束并把剩余缓冲写入文件
        if self.session.presistence is not None:
            await self.session.presistence.stop()
