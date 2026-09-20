import asyncio

from myagent.agent.hitl.base import HITLChannel
from myagent.agent.hitl.types import HumanChoice,HITLMessage,HumanReturn
from myagent.agent.execption import MaxStepsExceededError,ToolConsecutiveFailureError
from myagent.infra.events.payload import RequestErrorPayLoad
class HITL:
    """
    人在回路模块，当agent loop出现需要请求人类决策时使用该模块
    1. agent直接调用工具请求该模块（暂未实现，未来实现也不一定走这个模块）
    2. agent loop出现各种问题需要使用该模块（超过最大步数、熔断错误、输出截断max_token错误，工具命令权限请求）
    
    已知限制：
    由于当前项目并不包含前端，目前只有在lifeprism评估模块有一个控制台模拟路径作为输出，未来
    将会以SDK的形式注入到lifeprism系统，
    
    人在回路请求的前端接口以依赖注入hitl_channel，具体接口于HITL解耦实现

    具体各类hitl实现方式：
    1. 少量时直接在该类下实现
    2. 多量时在hitl/新建package文件夹存放各个类
    3. 需要通过配置设置时，增加注册机制。
    即当前HITL为一个总的控制，默认会挂在到全局/局部的context（已知限制：当前没有做agent隔离，多个agent使用同一套配置）
    
    注意：HITL注册的事件类型通常为waterfall实现控制，必须是协程

    """
    def __init__(self,hitl_channel:HITLChannel,grant_steps:int = 5,timeout:float = 60):
        """
        Args:
            hitl_channel: 请求人类应答的通道，具体实现注入（与 HITL 解耦）。
            grant_steps: 人类选"继续"时授予的额外步数。选"继续"必须真的能走下去，
                故授予量随裁决一起回给 loop——否则下一轮立刻再撞同一个上限。
            timeout: 等待人类应答的秒数，超时按"取消"收束，不把 loop 挂在等待上。
        """
        self.channel = hitl_channel
        self.grant_steps = grant_steps
        self.timeout = timeout

    async def maxstep_continue(self,payload:RequestErrorPayLoad,_next:callable):
        """
        当超过最大步数之后，会raise MaxStepsExceededError 由当前函数进行处理

        裁决的形状是"意图"而非"动作"：选"继续"时携带 grant（申请放宽 N 步预算），
        由 loop 落成 agent/grant 账本记录（见 ReActAgentLoop.hand_decision）——
        订阅方不直接改 loop 的状态。

        不管辖的错误必须 await 委托给链上下一个订阅方：waterfall 要求订阅方返回
        awaitable，漏掉 await 会让整条链的裁决静默丢失（见契约文档）。
        """
        if not isinstance(payload.error_type,MaxStepsExceededError):
            return await _next()
        message = HITLMessage(
            human_return_type="single-select",
            content="当前agent执行步骤达到最大步数，选择，",
            choices=[
                HumanChoice("继续","continue","选择'继续'agent将会继续执行当前指令"),
                HumanChoice("取消","break","若当前已经发送的其他命令取消后agent仍会继续执行"),
            ]
        )
        try:
            result : HumanReturn =await asyncio.wait_for(self.channel.ask_human(message),self.timeout)
            if result.choice_id == "continue":
                return {"decision": "continue","grant": {"steps": self.grant_steps}}
            else:
                return {"decision": "break"}
        except asyncio.TimeoutError:
            return {"decision": "break"}

    async def tool_breaker_continue(self,payload:RequestErrorPayLoad,_next:callable):
        """工具熔断的处置：raise_on_break 配置下，单个工具连续失败达阈值时
        ToolRegister 抛 ToolConsecutiveFailureError——含义是"该工具已不可用、
        需要外部介入"，故交人工决定这一步怎么走。

        错误形状要注意：熔断错与同批其他工具异常一起时，TaskGroup 会把它包成
        ExceptionGroup 上抛，payload.error_type 未必是熔断错本身，故先展开再判
        （见 _is_tool_breaker）。

        不管辖的错误必须 await 委托给链上下一个订阅方（见契约文档）。裁决的形状是
        "意图"而非"动作"：订阅方不直接改 loop 的状态，只表达 decision；终止是否按
        失败上报由 as_error 声明（见 ReActAgentLoop.hand_decision）。
        """
        if not self._is_tool_breaker(payload.error_type):
            return await _next()
        message = HITLMessage(
            human_return_type="single-select",
            content="工具连续失败已触发熔断，本轮内该工具不可用，选择：",
            choices=[
                HumanChoice("继续","continue","继续当前指令；熔断状态到本轮结束才复位，模型须改用其他工具"),
                HumanChoice("取消","break","终止当前执行，本轮按失败收尾"),
            ]
        )
        try:
            result : HumanReturn =await asyncio.wait_for(self.channel.ask_human(message),self.timeout)
            if result.choice_id == "continue":
                return {"decision": "continue"}
            return {"decision": "break","as_error": True}
        except asyncio.TimeoutError:
            return {"decision": "break","as_error": True}

    @staticmethod
    def _is_tool_breaker(error) -> bool:
        """判断错误是否（含嵌套地）为工具熔断错。

        TaskGroup 会把同批工具异常连同熔断错一起包成 ExceptionGroup，故群组要递归
        展开——只看最外层会把"包着的熔断"当成不认识的错误放过去。
        """
        if isinstance(error,BaseExceptionGroup):
            return any(HITL._is_tool_breaker(child) for child in error.exceptions)
        return isinstance(error,ToolConsecutiveFailureError)