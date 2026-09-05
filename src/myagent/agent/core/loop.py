from attr import dataclass

from myagent.agent import execption
from myagent.agent.core.session import session
from myagent.infra.events import EventService
from myagent.agent.core.tool import ToolRegister
from myagent.agent.core.provider import LLMProvider,ChatParams, LLMResponse,Message, StreamChunk, ToolCallRequest,Usage
from myagent.agent.core.session.types import (
    AssistantChunkData, SessionMetaData,ToolCallChunksData,AssistantMessageData, StepEndData,CompactionStartData,
    SessionRecordData,ReasoningChunksData,ToolCallData,ToolResultData,TurnEndData,CompactionSummaryData,
    TurnStartData,StepStartData,UserMessageData,ContentChunksData,RequestHeaderData,CompactionEndData
)
from myagent.agent.core.session.session import Session

from myagent.utils.time import now
import asyncio 
import logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
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


    def persist_now(self,loc:str):
        try:
            self._session.presistence.presist()
        except OSError as e:
            # 所有专门错误处理，类型等暂时先跳过
            logger.error(f"{loc} persist_now 出错{e}")
             

    async def _run_loop(self,message:Message)->LLMResponse|None:
        """对话循环主体：调 LLM → 记录 assistant 回复 → 执行工具并记录结果 → 循环。

        Args:
            message: 用户输入的消息，作为本轮第一条消息写入 session。

        event:
            llm_call -> stream chunk (<loop_llm_call_chunk>) -> <after_loop_llm_call> -> tool_use -> <after_tool_use>


            
        """

        try:
            # 注system prompt部分暂时没有做
            # turn start step start end 等4个可以写在外名，具体看需要之后需要什么数据，暂时写里面没有问题
            self._session.append("turn/start",TurnStartData())
            self._session.append("step/start",StepStartData())
            # TODO 暂时设置为initial 后续应该考虑resume change等
            self._session.append("request/header",RequestHeaderData(reason='initial',model_name=self._llm_client.model,system_prompt='',tools=self._tool_register.to_schemas(),params=self._llm_client.params))
            self._session.append("user/message",UserMessageData(message),surface_op="append")
            while True:
                
                response = None 
                chunks = []
                self.persist_now("before assistant/message")
                async for item in self._llm_client.stream_chat(self._session.derive_messages(),self._tool_register.to_schemas()):
                    if isinstance(item, LLMResponse):
                        response = item
                        self._session.append("assistant/message",AssistantMessageData(Message(
                            role = "assistant",
                            content = response.content,
                            tool_calls=response.tool_call_requests,
                            reasoning_content=response.reasoning_content,
                        ),usage=response.usage),surface_op="append")
                    else:
                        chunks.append(item)
                        self._session.append("assistant/chunk",AssistantChunkData(item))
                if response and response.tool_call_requests:
                    for tool_call in response.tool_call_requests:
                        id = tool_call.id
                        name = tool_call.name
                        arguments = tool_call.arguments

                        self._session.append("tool/call",ToolCallData(tool_name=name,call_id = id ,arguments=arguments))
                        self.persist_now("tool/call")
                        # TODO 工具出错相关处理
                        result = await self._tool_register.execute(name,**arguments if arguments else None )
                        self._session.append("tool/result",ToolResultData(call_id=id,tool_name=name,message=Message(role="tool",content=result,tool_call_id=id)),surface_op="append")
                    self._session.append("step/end",StepEndData())
                    self.persist_now("step/end")
                    self._session.append("step/start",StepStartData())
                else:
                    self._session.append("turn/end",TurnEndData('success'))
                    return response
            
        except asyncio.CancelledError:
           pass 

        except asyncio.TimeoutError:
            pass
            
    def handle_cancel(session:Session , response:LLMResponse|None=None ,chunks:list[StreamChunk]|None=None ,tool_call_requests :list[ToolCallRequest]|None = None,):
        # 先判断中断之后进行到哪一步了
        pass 
        # if response is None and chunks is None:
        #     # llm 还未开始调用
        #     return None 
        # elif response is None and chunks is not None:
        #     # llm开始调用但未完成，仅仅组装content
        #     content = ""
        #     for chunk in chunks:
        #         content +=chunk.content
        #     if content :
        #         session.add_message(Message(role="assistant",content=content))
        #         return LLMResponse(
        #             content = content,
        #             usage=Usage(),
        #             interrupted=False
        #         )
        #     else : 
        #         return None 
        # elif response and  tool_call_requests is None:
        #     # llm调用结束，且无工具调用，直接返回
        #     return response
        # elif response and tool_call_requests :
        #     # 已经记录的工具调用id
            

