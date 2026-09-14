from dataclasses import dataclass
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
    TurnStartData,StepStartData,UserMessageData,ContentChunksData,RequestHeaderData,CompactionEndData,
    LLMRetryData
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

@dataclass(frozen=True)
class RetryPolicy:
    """一档重试的退避参数：第 n 次重试等待 min(base_delay * multiplier^(n-1), cap) 秒。

    参数值由策略注册表（LLMRerty）以 dict 形式给出，loop 在此承接为本结构再计算
    退避。暂放 loop，策略扩展后（如新增抖动、按环境差异化）再定该结构的归属。
    """
    base_delay: float = 0.0  # 基准值：第 1 次重试的等待秒数
    multiplier: float = 1.0  # 乘数：每次重试等待时长相对上一次的放大倍数
    cap: float = float("inf")  # 上限：等待时长封顶值（秒），默认不封顶

class ReActAgentLoop:
    def __init__(
        self,
        event_service:EventService,
        session :Session,
        system_prompt : SystemPrompt,
        agent_config :AgentConfig,
        llm_client:LLMProvider,
        name :str | None = None,
        prompt_render_parame :dict |None = None):     
        """        
        Args:
            event_service: 事件服务，用于在循环关键节点发布事件（埋点尚未接入）。
            session: 本循环绑定的会话，所有消息读写都落在它上面。
            llm_client: LLM 调用实现（LLMProvider 接口）。
        """
        self.name =name  if name else str(uuid.uuid4())[:8] # 若没有名称/id , 
        self._event_service = event_service
        # 工具注册表由 loop 自持（非依赖注入）：外部通过 loop.tool_register.register(...) 注册工具
        self.tool_register = ToolRegister()
        self.system_prompt = system_prompt
        self._llm_client = llm_client
        self.agent_config = agent_config
        self.prompt_render_parame = prompt_render_parame
        self.inbox = {"next_turn":[],"next_step":[]} # next_turn 的消息要等待agentturn完成之后才会调用，而next_step会在下一个step马上打断并调用
        self._session = session
        self._task = None
        self.state :Literal["idle","running","maintenance"] = "idle"# 暂时没用
        self._LLM_CALL_TIMEOUT = LLM_CALL_TIMEOUT
        # 工具熔断状态随 turn 结束清空：接线在此（loop 同时持有 event_service
        # 与 tool_register），清空逻辑在 ToolRegister.reset_breaker
        self._event_service.register(TURN_END.name, self.tool_register.reset_breaker)
        # 事件循环
        self._wakeup = asyncio.Event()

    def start(self) -> asyncio.Task:
        """启动后台循环：整个 loop 对应一个 task，由 _task 持有直到结束。

        turn 在 _loop 内被直接 await，不再每轮单独建 task——turn 是循环内的
        一段顺序执行，不是并发单元；每轮建 task 只会让 _task 被反复覆盖、
        cancel 失去稳定目标。已在运行时直接返回现有 task，避免跑出两个 _loop。
        """
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_loop_done)
        return self._task

    def cancel(self):
        """取消当前在途 turn、作废 inbox 中排队的消息，并结束循环。

        与 asyncio 原设计对齐：取消就是要停下来。取消异常由 step/turn 记录
        interrupted 后原样上抛，最终在 _loop 统一捕获：记一条 warning 后 break，
        让任务以正常结束（而非 cancelled）收场，不把取消影响扩散给外部等待方。
        之后再 send() 会按需重新启动循环（见 send）。
        """
        if self._task is None:
            return
        self._task.cancel()
        # 清空inbox
        self.inbox = {"next_turn":[],"next_step":[]}

    async def _loop ( self ): 
        while True : 
            try : 
                await self ._wakeup.wait() # 无消息时挂起，不占 CPU 
                self ._wakeup.clear() 
                while self .inbox[ "next_step" ]: 
                    # 先处理高优先级的 next_step 
                    await self .turn( self .inbox[ "next_step" ].pop( 0 ))
                while self .inbox[ "next_turn" ]: 
                    await self .turn( self .inbox[ "next_turn" ].pop( 0 ))
            except asyncio.CancelledError:
                # 取消 = 结束循环。吞掉取消后协程不会被标记为 cancelled，while
                # 也不会自己停，所以必须在此显式 break；在途的取消已由 step/turn
                # 记好 interrupted 终态，并原样上抛到这里
                logger.warning("agent loop 收到取消：打断循环，任务结束")
                break

    def _on_loop_done(self,task:asyncio.Task):
        """loop 任务结束回调：取回 task 异常，防止静默失败。

        create_task 产出的 task 若无人 await、也无人取异常，异常会被 asyncio
        吞掉（仅在 task 被 GC 时打印 "Task exception was never retrieved"）。
        start() 的调用方通常也不 await 该 task，本回调是这一异常的唯一出口：
        - 正常结束（含因取消 break）：exc 为 None，无需处理
        - 有异常：记 error 日志（带堆栈），使失败在日志中可见

        注意：取回异常本身也会抑制 asyncio 的 "never retrieved" 警告，
        因此这里必须保证异常不被丢弃。
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger.error(f"agent loop 任务异常终止：{exc!r}",exc_info=exc)
    
    
    async def send(self,user_prompt : str | list,send_type : Literal["next_turn","next_step"]="next_turn"):
        """
        将消息发送进inbox
        arg :
            user_prompt : user消息
            send_type : 消息发送类型，存储进turn 还是step，目前无论选turn还是step都会进入turn（暂时）
        """
        # send 是唯一入口：循环可能从未启动、已被 cancel 停掉、或因异常终止，
        # 此时入队将无人消费（消息静默积压），所以先确认循环活着；只入队不消费
        # 就是故障，按需重启比"停下即失效"更省心（循环停止期间不占资源）
        if self._task is None or self._task.done():
            self.start()
        # 只入队原始用户消息：system prompt / system reminder / runtime context
        # 由 _request_header 在组装请求时统一处理（此处不组装，避免重复 assemble）
        content = [{"type":"text","text":user_prompt}] if isinstance(user_prompt,str) else user_prompt
        self.inbox["next_turn"].append(Message(role= "user",content=content))
        self._wakeup.set()
    def followup(self):
        pass

    

    def persist_session_now(self):
        try:
            self._session.presistence.presist()
        except OSError as e:
            # 所有专门错误处理，类型等暂时先跳过
            pass 

    async def turn(self,user_message):
        # 默认成功：step 正常返回时以其结果收口；step 抛出未归一化的异常时，
        # 由下面的 except Exception 先归一化 result 再原样上抛（异常仍要暴露给
        # _on_loop_done 记录，不能静默吞掉）
        result = StepOut(reason_type="success",reason_text="")
        try:
            self._session.append("turn/start",TurnStartData())
            self._event_service.trigger(TURN_START,TurnStartPayload())
            result = await self.step(user_message)
        except asyncio.CancelledError:
            # 取消原样上抛（与 asyncio 原设计对齐）：先记下本 turn 的 interrupted
            # 终态（否则 finally 会写成 success），再由 _loop 统一捕获并结束循环。
            # 这里不能吞掉——吞掉后取消传播不到 _loop，循环就不会被打断
            result = StepOut(reason_type="interrupted",reason_text="用户手动取消")
            raise
        except Exception as e:
            # step 未归一化的异常（如 request/error 无人认领抛出的 LLmError、
            # 非熔断的 ExceptionGroup）：先归一化 result，否则 finally 会把本轮
            # 记成 success，污染终态；异常原样上抛交由 _on_loop_done 记录
            logger.error(f"turn 未归一化异常：{e!r}",exc_info=e)
            result = StepOut(reason_type="error",reason_text=str(e))
            raise
        finally:
            # 一轮的结果由 step 汇总：出现过未恢复的 error 则为 error 并带上错误
            # 信息；用户取消则为 interrupted
            self._session.append("turn/end",TurnEndData(reason_type=result.reason_type,reason_text=result.reason_text))
            self._event_service.trigger(TURN_END,TurnEndPayload())
    
    def _request_header(self,user_message : Message | None) -> list[Message]:
        """组装本 step 的完整请求消息列表（每 step 唯一一次提示词组装点）。

        产出的 Message List 布局：第 1 位 System Prompt、第 2 位 System Reminder
        （role=user，为空则省略），其后是 session 的会话消息面。

        三类内容的持久化落点（保证每一步输入可复现）：
        - System Prompt / System Reminder 不落 session 的 message list，以
          request/header 事件落盘（仅首次与配置变更时写入）
        - runtime context 合并进当前用户消息后落 user/message

        args:
            user_message: 本 step 的用户消息；工具调用循环的后续 step 为 None
        return:
            完整请求消息列表
        """
        # 提示词组装只在此处发生一次
        assembly_prompt : AssemblyPrompt = self.system_prompt.assemble(self.name)
        system_prompt = self.system_prompt.render(assembly_prompt,self.prompt_render_parame)
        system_reminder = assembly_prompt.system_reminder

        # 当前用户消息：合并 runtime context 后落盘（append-only，输入即落盘）
        if user_message:
            merged = self._merge_runtime_context(user_message,assembly_prompt.context)
            self._session.append("user/message",UserMessageData(merged),surface_op="append")
            self._event_service.trigger(USER_MESSAGE,UserMessagePayload())

        current = RequestHeaderData(
            reason="initial",
            model_name=self._llm_client.model,
            system_prompt=system_prompt,
            system_reminder=system_reminder,
            tools=self.tool_register.to_schemas(),
            params=self._llm_client.params,
        )
        # 与最近一条 request/header 比较（reason 不参与比较，它是写入时才决定的结果）
        last = self._session.latest_request_header()
        if last is None:
            # 本会话从未写入过配置快照
            self._session.append("request/header", current)
            self._event_service.trigger(REQUEST_HEADER,RequestHeaderPayload())
        elif (last.model_name, last.system_prompt, last.system_reminder, last.tools, last.params) != (current.model_name, current.system_prompt, current.system_reminder, current.tools, current.params):
            current.reason = "change"
            self._session.append("request/header", current)
            self._event_service.trigger(REQUEST_HEADER,RequestHeaderPayload())
        # 除 reason 外完全一致：不写入，request/header 仅首次和配置变更时记录

        messages : list[Message] = []
        if system_prompt:
            messages.append(Message(role="system",content=system_prompt))
        if system_reminder:
            messages.append(Message(role="user",content=f"<system-reminder>{system_reminder}</system-reminder>"))
        messages.extend(self._session.derive_messages())
        return messages

    @staticmethod
    def _merge_runtime_context(message : Message,runtime_context : str) -> Message:
        """把 runtime context 合并进当前用户消息（作为追加的文本块）。

        runtime context 是运行过程中动态生成的标注，必须随输入落盘才能保证会话
        可复现；无内容时原样返回，不产生多余文本块。
        """
        if not runtime_context:
            return message
        content = [{"type":"text","text":message.content}] if isinstance(message.content,str) else list(message.content)
        content.append({"type":"text","text":runtime_context})
        return Message(
            role=message.role,
            content=content,
            tool_calls=message.tool_calls,
            tool_call_id=message.tool_call_id,
            reasoning_content=message.reasoning_content,
        )

    async def step(self,user_message) -> StepOut:
        step_error = None
        step_count = 0
        # step 的汇总结果（turn 据此写 turn/end）：任一轮以 error 结束即为 error；
        # 中间轮次的重试不算——重试后仍继续，最终成功则整轮为 success
        result = StepOut(reason_type="success",reason_text="")
        while True: # 为了重试而添加的循环，try写在内部判断究竟是什么错误来决定是否重试
            # 本轮（一条 step/end 记录）的结束原因，默认正常结束
            round_reason_type, round_reason_text = "success", ""
            # 步数兜底：达到上限仍未收敛（熔断管不到的场景，如模型轮换调用多个
            # 工具、每个都不达熔断阈值），强制终止，防止 while True 死循环
            if step_count >= self.agent_config.step_limit:
                limit_error = RuntimeError(f"达到最大步数{self.agent_config.step_limit}，强制终止本turn")
                self._event_service.trigger(REQUEST_ERROR,RequestErrorPayLoad(error_type=limit_error))
                result = StepOut(reason_type="error",reason_text=str(limit_error))
                break
            step_count += 1
            try:
                self._session.append("step/start",StepStartData())
                self._event_service.trigger(STEP_START,StepStartPayload())
                # 组装本 step 的完整请求：用户消息（含 runtime context）落盘 +
                # request/header 快照 + 完整消息列表（System Prompt/Reminder 置前）
                messages = self._request_header(user_message)
                user_message = None # 用户消息只写首个step，后续step（工具循环）不再重复写
                # 在模型请求前强制保存session
                self.persist_session_now()
                response = await asyncio.wait_for(self._ask_model(messages),self._LLM_CALL_TIMEOUT)

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
                        self._session.append("tool/result",ToolResultData(call_id=tool_call.id,tool_name=tool_call.name,message=Message(role="tool",content=tool_result.content,tool_call_id=tool_call.id,),is_error=tool_result.is_error,duration_ms=tool_result.duration_ms),surface_op="append",source_event_seqs=[])
                        # 事件负载与 session 记录同源，供评估/观测订阅（如 ToolEvaluate）消费
                        self._event_service.trigger(TOOL_RESULT,ToolResultPayload(
                            tool_name=tool_call.name,
                            arguments=tool_call.arguments,
                            is_error=tool_result.is_error,
                            error_type=tool_result.error_type.value if tool_result.error_type else None,
                            content=tool_result.content,
                            duration_ms=tool_result.duration_ms,
                        ))
                else:
                    break # 模型不再请求工具，本轮结束（ReAct 终止条件）
                
            except TimeoutError as e: # python 3.11+ 用TimeoutError， < 3.11用asyncio.TimeoutError
                step_error = e # 这里暂时先就这样简单的写，后续才丰富step_error的内容

            except asyncio.CancelledError:
                # 取消中断比较特殊，单独处理，不走request/error（不是 LLM 调用错误）：
                # 本 step 记为 interrupted 后原样上抛（不能只 break——那只是结束本轮，
                # 取消会被吞掉、_loop 收不到，循环就不会被打断），终态由 finally
                # 落 step/end、再由 turn 汇总写 turn/end
                round_reason_type, round_reason_text = "interrupted","用户手动取消"
                result = StepOut(reason_type="interrupted",reason_text="用户手动取消")
                raise

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
                    # 非熔断错误不属于本处理范围，原样上抛；先归一化本轮终态，
                    # 否则 finally 会把它记成 success（与 turn/end 同源的问题）
                    round_reason_type, round_reason_text = "error", str(eg)
                    raise
                if rest is not None:
                    logger.warning(f"熔断处理时忽略同批其他异常: {rest!r}")
                step_error = matched.exceptions[0]

            except LLMCallError as e:
                step_error = e
            except Exception as e:
                # 未归一化异常（provider 内部 bug、session.append 的 ValueError/
                # TypeError、_request_header 渲染异常等）：没有专门分支接住，
                # 同样先修正本轮终态再原样上抛，避免 finally 记成 success
                round_reason_type, round_reason_text = "error", str(e)
                raise
            finally:
                try:
                    if step_error :
                        exhausted_reason = await self._handle_step_error(step_error)
                        # 重试耗尽：本轮记为 error 并结束整个 step 循环。不在 finally 内
                        # break（会抑制在途异常），只置 result，由循环末尾统一退出
                        if exhausted_reason is not None:
                            round_reason_type, round_reason_text = "error", exhausted_reason
                            result = StepOut(reason_type="error",reason_text=exhausted_reason)
                        step_error = None  # 一次错误只触发一次 request/error，避免后续轮次重复处理
                except Exception as e:
                    # 重试决策自身抛错（如 request/error 无人认领抛出的 LLmError）：
                    # 同样先修正本轮终态再原样上抛
                    round_reason_type, round_reason_text = "error", str(e)
                    raise
                finally:
                    # 内层 finally 兜底：无论上面是否抛错，step/end 都必须落盘，
                    # 否则该轮在 session 里完全不可见（LLmError 路径原来会丢记录）
                    self._session.append("step/end",StepEndData(reason_type=round_reason_type,reason_text=round_reason_text))
                    self._event_service.trigger(STEP_END,StepEndPayload())
            if result.reason_type == "error":
                break  # 终态错误（重试耗尽等）：结束本 step
        return result
   
    
    async def _ask_model(self,messages : list[Message])->LLMResponse:
        """
        请求模型，并触发assistant/message 和 assistant/chunk事件
        args:
            messages: 本 step 的完整请求消息列表（由 _request_header 组装）
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
            async for item in self._llm_client.stream_chat(messages,self.tool_register.to_schemas()):
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

    async def _handle_step_error(self,error)->str | None:
        """处理一次 step 错误，返回终止原因文本（None 表示不终止、进入下一轮）。

        触发 request/error（waterfall 语义的控制反转挂点）拿订阅方决策：
        - 无决策：视为无人认领，抛 LLmError（设计语义）
        - retry / backoff_retry：记 llm/retry 后按策略退避等待，进入下一轮重试；
          次数达到 max_retry_count 时不再重试，返回终止原因文本（形如
          "10/10 达到最大重试错误：429 限流..."，保留最后一次原始错误信息，
          供 step 记为 error、界面直接展示）
        - 其余（如 dont_retry）：不等待，维持既有控制流（由 step_limit 兜底）
        """
        request_error_result = self._event_service.trigger(REQUEST_ERROR,RequestErrorPayLoad(error_type=error))
        # waterfall 无订阅方/订阅方未返回决策时 trigger 返回 None，
        # 视为"无人认领"走 LLmError（设计语义），而不是 AttributeError
        decision = (request_error_result or {}).get("decision",None)
        if decision is None:
            raise LLmError(f"错误无人认领") from error
        logger.info(f"llm请求错误决策：{decision}；错误{error}")
        if decision not in ("retry","backoff_retry"):
            return None
        # 第 n 次重试：n = 本 turn 已记录的重试次数 + 1
        attempt = self._session.llm_retry_count(self._session.turn) + 1
        max_retry_count = self.agent_config.max_retry_count
        if attempt > max_retry_count:
            logger.warning(f"重试次数已达上限 {max_retry_count}，结束本 step：{error}")
            return f"{max_retry_count}/{max_retry_count} 达到最大重试错误：{error}"
        # 先落 llm/retry 记录再等待：该记录是"决定重试"的事实，也是下轮 attempt 计数来源
        self._session.append("llm/retry",LLMRetryData(retry_count=attempt,reason=decision))
        policy = RetryPolicy(**(request_error_result.get("policy") or {}))
        await self.retry_delay(error,policy,attempt)
        return None

    async def retry_delay(self,error,policy:RetryPolicy,attempt:int)->float:
        """计算并等待第 attempt 次重试的退避时长，返回实际等待秒数。

        delay = min(base_delay * multiplier^(attempt-1), cap)；错误携带服务端
        Retry-After 建议时取其与本地退避的较大值（服务端更清楚何时可用，本地
        退避作为下限）。等待用 asyncio.sleep，不阻塞事件循环。

        args:
            error: 本次错误，用于取 details["retry_after"]；非领域异常（如
                TimeoutError）没有 details，取不到建议值，仅用本地退避
            policy: 策略注册表给出的该档退避参数
            attempt: 第几次重试，从 1 开始
        """
        delay = min(policy.base_delay * policy.multiplier ** (attempt - 1),policy.cap)
        retry_after = (getattr(error,"details",None) or {}).get("retry_after")
        if isinstance(retry_after,(int,float)):
            delay = max(delay,retry_after)
        if delay > 0:
            await asyncio.sleep(delay)
        return delay

    
