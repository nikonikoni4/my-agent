from myagent.agent import execption
from myagent.infra.events import EventService
from myagent.agent.core.tool import ToolRegister
from myagent.agent.core.provider import LLMProvider,ChatParams, LLMResponse,Message
from myagent.agent.core.session import Session
from myagent.utils.time import now
import asyncio 
class Loop:
    """单个会话的对话循环：调用 LLM → 记录回复 → 执行工具，直到模型不再请求工具。

    一个实例在构造时绑定一个 session，只服务该会话的生命周期；
    切换会话应创建新的 loop 实例，而不是修改本实例的 session。

    触发事件: TODO（事件埋点尚未接入，event_service 目前仅保存未使用）
    """

    def __init__(self,event_service:EventService,session :Session,tool_register:ToolRegister,llm_client: LLMProvider ):
        """保存循环运行所需的依赖，全部由外部注入。

        Args:
            event_service: 事件服务，用于在循环关键节点发布事件（埋点尚未接入）。
            session: 本循环绑定的会话，所有消息读写都落在它上面。
            tool_register: 工具注册表，提供工具 schema 与执行。
            llm_client: LLM 调用实现（LLMProvider 接口）。
        """
        self._event_service = event_service
        self._tool_register = tool_register
        self._llm_client = llm_client
        self._session = session
        self._task = None

    def cancel(self):
        """请求打断当前正在运行的对话回合（若存在）。

        未在运行时静默返回，不报错。
        """
        if self._task:
            self._task.cancel()

    async def followup(self,message:Message)->LLMResponse:
        """向会话追加用户消息并启动一轮对话循环，等待其完成。

        Args:
            message: 用户输入的消息。

        
        """
        self._task = asyncio.create_task(self._run_loop(message))
        return await self._task


    async def _run_loop(self,message:Message)->LLMResponse:
        """对话循环主体：调 LLM → 记录 assistant 回复 → 执行工具并记录结果 → 循环。

        Args:
            message: 用户输入的消息，作为本轮第一条消息写入 session。

        Returns:
            
        """
        try:
            self._session.add_message(message) # 加入第一条消息
            while True:
                response = await self._llm_client.chat(self._session.messages,self._tool_register.to_schemas())
                self._session.add_message(Message(
                    role = "assistant",
                    content = response.content,
                    tool_calls=response.tool_call_requests,
                    reasoning_content=response.reasoning_content,
                ))
                if response.tool_call_requests:
                    for tool_call in response.tool_call_requests:
                        result = await self._tool_register.execute(tool_call.name,**tool_call.arguments)
                        self._session.add_message(Message(
                            role="tool",
                            content=result,
                            tool_call_id=tool_call.id
                        ))
                else:
                    return response
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            pass
            