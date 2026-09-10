from typing import Literal
import uuid
from myagent.agent import execption
from myagent.agent.core.agent.types import AgentConfig, StepOut
from myagent.agent.core.session import session
from myagent.agent.core.systemprompt import AssemblyPrompt, SystemPrompt
from myagent.infra.events import EventService
from myagent.agent.core.tool.register import ToolRegister
from myagent.agent.execption import LLMCallError, LLmError, ToolConsecutiveFailureError
from myagent.agent.core.provider import LLMProvider,ChatParams, LLMResponse,Message, StreamChunk,Usage
from myagent.agent.core.session.types import (
    AssistantChunkData, SessionMetaData,ToolCallChunksData,AssistantMessageData, StepEndData,CompactionStartData,
    SessionRecordData,ReasoningChunksData,ToolCallData,ToolResultData,TurnEndData,CompactionSummaryData,
    TurnStartData,StepStartData,UserMessageData,ContentChunksData,RequestHeaderData,CompactionEndData
)
from myagent.agent.core.session.session import Session

import asyncio 
import logging

from myagent.infra.events.eventspec import (
    TURN_START, STEP_START, REQUEST_HEADER, USER_MESSAGE,
    ASSISTANT_CHUNK, ASSISTANT_MESSAGE, TOOL_CALL, TOOL_RESULT,
    STEP_END, TURN_END, REQUEST_ERROR,
)
from myagent.infra.events.payload import (
    TurnStartPayload, StepStartPayload, RequestHeaderPayload, UserMessagePayload,
    AssistantChunkPayload, AssistantMessagePayload, ToolCallPayload, ToolResultPayload,
    StepEndPayload, TurnEndPayload, RequestErrorPayLoad,
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
LLM_CALL_TIMEOUT = 120 # 单位秒
TOOL_CALL_TIMEOUT = 60 # 单位秒
MAX_RETRY_COUNT= 5 # 次
class ReActAgentLoop:
    def __init__(
        self,
        event_service:EventService,
        session :Session,
        tool_register:ToolRegister,
        system_prompt : SystemPrompt,
        agent_config :AgentConfig,
        llm_client:LLMProvider,
        name :str | None = None,
        prompt_render_parame :dict |None = None):     
        """        
        Args:
            event_service: 事件服务，用于在循环关键节点发布事件（埋点尚未接入）。
            session: 本循环绑定的会话，所有消息读写都落在它上面。
            tool_register: 工具注册表，提供工具 schema 与执行。
            llm_client: LLM 调用实现（LLMProvider 接口）。
        """
        self.name =name  if name else str(uuid.uuid4())[:8] # 若没有名称/id , 
        self._event_service = event_service
        self.tool_register = tool_register
        self.system_prompt = system_prompt
        self._llm_client = llm_client
        self.agent_config = agent_config
        self.prompt_render_parame = prompt_render_parame
        self.inbox = {"next_turn":[],"next_step":[]} # next_turn 的消息要等待agentturn完成之后才会调用，而next_step会在下一个step马上打断并调用
        self._session = session
        self._task = None
        self.state :Literal["idle","running","maintenance"] = "idle"# maintenance 暂时没用
        self._LLM_CALL_TIMEOUT = LLM_CALL_TIMEOUT
        # 工具熔断状态随 turn 结束清空：接线在此（loop 同时持有 event_service
        # 与 tool_register），清空逻辑在 ToolRegister.reset_breaker
        self._event_service.register(TURN_END.name, self.tool_register.reset_breaker)

    async def send(self,user_prompt : str | list):
        """
        将消息发送进inbox
        """
        if isinstance(user_prompt,str):
            content = [{"type":"text","text":user_prompt}]
        # 整合可能会动态变化的系统提示词(为了缓存命中而不放在开头)
        # 这里暂时不拼接
        await self.turn(Message("user",content))

    def followup(self):
        pass


    def persist_session_now(self):
        try:
            self._session.presistence.presist()
        except OSError as e:
            # 所有专门错误处理，类型等暂时先跳过
            pass 

    async def turn(self,user_message):
        try:
            self._session.append("turn/start",TurnStartData())
            self._event_service.trigger(TURN_START,TurnStartPayload())
            await self.step(user_message)
        finally:
            self._session.append("turn/end",TurnEndData("success"))  # 暂时这么写
            self._event_service.trigger(TURN_END,TurnEndPayload())
    def _request_header(self):
        # 组装systemprompt
        assembly_prompt : AssemblyPrompt = self.system_prompt.assemble(self.name)
        system_prompt = self.system_prompt.render(assembly_prompt,self.prompt_render_parame)
        current = RequestHeaderData(
            reason="initial",
            model_name=self._llm_client.model,
            system_prompt=system_prompt,
            tools=self.tool_register.to_schemas(),
            params=self._llm_client.params,
        )
        # 与最近一条 request/header 比较（reason 不参与比较，它是写入时才决定的结果）
        last = self._session.latest_request_header()
        if last is None:
            # 本会话从未写入过配置快照
            self._session.append("request/header", current)
            self._event_service.trigger(REQUEST_HEADER,RequestHeaderPayload())
        elif (last.model_name, last.system_prompt, last.tools, last.params) != (current.model_name, current.system_prompt, current.tools, current.params):
            current.reason = "change"
            self._session.append("request/header", current)
            self._event_service.trigger(REQUEST_HEADER,RequestHeaderPayload())
        # 除 reason 外完全一致：不写入，request/header 仅首次和配置变更时记录

    async def step(self,user_message):
        step_error = None
        step_count = 0
        while True: # 为了重试而添加的Ture，try写在内部判断究竟是什么错误来决定是否重试
            # 步数兜底：达到上限仍未收敛（熔断管不到的场景，如模型轮换调用多个
            # 工具、每个都不达熔断阈值），强制终止，防止 while True 死循环
            if step_count >= self.agent_config.step_limit:
                step_error = RuntimeError(f"达到最大步数{self.agent_config.step_limit}，强制终止本turn")
                self._event_service.trigger(REQUEST_ERROR,RequestErrorPayLoad(error_type=step_error))
                break
            step_count += 1
            try:
                self._session.append("step/start",StepStartData())
                self._event_service.trigger(STEP_START,StepStartPayload())
                self._request_header()
                if user_message:
                    self._session.append("user/message",UserMessageData(user_message),surface_op="append")
                    self._event_service.trigger(USER_MESSAGE,UserMessagePayload())
                    user_message = None # 用户消息只写首个step，后续step（工具循环）不再重复写
                # 在模型请求前强制保存session
                self.persist_session_now()
                response = await asyncio.wait_for(self._ask_model(),self._LLM_CALL_TIMEOUT)

                # 工具调用循环前强制保存session
                self.persist_session_now()
                if response.tool_call_requests:
                    async with asyncio.TaskGroup() as tg:
                        tasks = []
                        for tool_call in response.tool_call_requests:
                            # 传入原始调用（arguments 为 wire JSON 字符串），
                            # 解析与校验都在 ToolRegister（信任边界）内完成
                            tasks.append(tg.create_task(self.tool_register.execute(tool_call)))
                            self._session.append("tool/call",ToolCallData(tool_name=tool_call.name,call_id = tool_call.id ,arguments=tool_call.arguments))
                            self._event_service.trigger(TOOL_CALL,ToolCallPayload())
                    for index,task in enumerate(tasks):
                        tool_call = response.tool_call_requests[index]
                        tool_result =  task.result()
                        # 回喂内容与截断/语法话术均由工具层产出（ToolResult.content），
                        # loop 只负责把结果按 tool_call_id 配对写回
                        self._session.append("tool/result",ToolResultData(call_id=tool_call.id,tool_name=tool_call.name,message=Message(role="tool",content=tool_result.content,tool_call_id=tool_call.id,),is_error=tool_result.is_error),surface_op="append",source_event_seqs=[])
                        self._event_service.trigger(TOOL_RESULT,ToolResultPayload())
                else:
                    break # 模型不再请求工具，本轮结束（ReAct 终止条件）
                
            except TimeoutError as e: # python 3.11+ 用TimeoutError， < 3.11用asyncio.TimeoutError
                step_error = e # 这里暂时先就这样简单的写，后续才丰富step_error的内容

            except asyncio.CancelledError:
                # 取消中断比较特殊，单独处理，不走request/error
                pass

            except ExceptionGroup as eg:
                # TaskGroup 把子任务（工具执行）异常包成 ExceptionGroup 上抛。
                # register.execute 已兜掉普通工具异常，这里只识别熔断抛错
                # （raise_on_break）：记入 step_error 即可。注意 except 不会中断
                # while（捕获后控制流回到循环顶部，同 TimeoutError 的重试路径），
                # 是否终止由 REQUEST_ERROR 的 waterfall（IOC）控制信号决定，
                # 信号细节当前未定；无订阅方时循环继续，由 step_limit 兜底
                # 已知限制，这里只会获取第一个group的错误，后续的错误会静默失败
                matched, rest = eg.split(ToolConsecutiveFailureError)
                if matched is None:
                    raise  # 非熔断错误不属于本处理范围，原样上抛
                if rest is not None:
                    logger.warning(f"熔断处理时忽略同批其他异常: {rest!r}")
                step_error = matched.exceptions[0]

            except LLMCallError as e:
                step_error = e 
            finally:
                if step_error :
                    # request/error 是 waterfall 语义事件：控制反转挂点，后续
                    # 订阅方（如人在回路处理）经返回值给出控制信号，当前无订阅方
                    request_error_result = self._event_service.trigger(REQUEST_ERROR,RequestErrorPayLoad(error_type=step_error))
                    # waterfall 无订阅方/订阅方未返回决策时 trigger 返回 None，
                    # 视为"无人认领"走 LLmError（设计语义），而不是 AttributeError
                    decision = (request_error_result or {}).get("decision",None)
                    if decision:
                        logger.info(f"llm请求错误决策：{decision}；错误{step_error}")
                    else:
                        raise LLmError(f"错误无人认领") from step_error
                    step_error = None  # 一次错误只触发一次 request/error，避免错误后的重试轮次里重复触发
                # 先不管错误处理，等先跑通了一遍流程之后再逐个错误处理进行安排，假设当前不会出错
                self._session.append("step/end",StepEndData())
                self._event_service.trigger(STEP_END,StepEndPayload())




                 
    
    async def _ask_model(self)->LLMResponse:
        """
        请求模型，并触发assistant/message 和 assistant/chunk事件
        return : 
            LLMResponse
        raise:
            LLMCallError

        在内部捕获LLMCallError，补齐由于llmcallerror导致的session缺少：
        补齐策略：
        1. 所有的error的已经输出的chunk不回滚，保留。原则是：已经落盘的内容不会在被修改，append-only
        2. 若缺少finish chunk则补齐finish chunk，finish_reason = error
        3. 原有的json解析和max_token错误已经移动到工具调用部分处理，见docs\adr\2026-09-10-工具调用解析与截断处置移入工具层.md
        4. 不补齐assistant/message
        """
        finish_emitted = False
        try:
            async for item in self._llm_client.stream_chat(self._session.derive_messages(),self.tool_register.to_schemas()):
                if isinstance(item, LLMResponse):
                    response = item
                    self._session.append("assistant/message",AssistantMessageData(Message(
                        role = "assistant",
                        content = response.content,
                        tool_calls=response.tool_call_requests,
                        reasoning_content=response.reasoning_content,
                    ),usage=response.usage),surface_op="append",source_event_seqs=[])
                    self._event_service.trigger(ASSISTANT_MESSAGE,AssistantMessagePayload())
                else:
                    if item.finish_reason is not None:
                        finish_emitted = True
                    self._session.append("assistant/chunk",AssistantChunkData(item))
                    self._event_service.trigger(ASSISTANT_CHUNK,AssistantChunkPayload())
        except LLMCallError:
            # 调用失败：已产出的 chunk 一律保留（append-only）。正常结束时 provider
            # 已产出 finish 块，这里只为缺失的情况补一个 error 结束原因，使日志上
            # "正常结束"与"调用失败"可判别；不可回滚、不补 assistant/message
            if not finish_emitted:
                self._session.append("assistant/chunk",AssistantChunkData(StreamChunk(finish_reason="error")))
                self._event_service.trigger(ASSISTANT_CHUNK,AssistantChunkPayload())
            raise
 
        return response
