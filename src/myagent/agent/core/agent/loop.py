from dataclasses import dataclass
from typing import Literal
import uuid
from myagent.agent.core.agent.types import AgentConfig, FinalResult
from myagent.agent.core.systemprompt import AssemblyPrompt, SystemPrompt
from myagent.infra.events import EventService
from myagent.agent.core.tool.register import ToolRegister
from myagent.agent.execption import AgentUnclaimedError, LLMCallError, MaxStepsExceededError, RetryExhaustedError
from myagent.agent.core.provider import LLMProvider,LLMResponse,Message
from myagent.agent.core.session.types import (
    AssistantChunkData,AssistantMessageData, StepEndData,
    ToolCallData,ToolResultData,TurnEndData,
    TurnStartData,StepStartData,UserMessageData,RequestHeaderData,
    LLMRetryData,AgentGrantData,
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
LLM_CALL_TIMEOUT = 120
# 异常链渲染的层数上限：当前错误链最长 3 层（httpx->LLMConnectionError->AgentUnclaimedError）
ERROR_CHAIN_MAX_DEPTH = 3
# 需要"继续下一轮"的决策；其余决策一律结束本 turn 的循环
RETRY_DECISIONS = ("retry","backoff_retry")
LOOP_CONTROL = ("continue","break")

def _collect_error_lines(error:BaseException,lines:list,depth:int,indent:str) -> None:
    """把一个异常及其起因链展开成带缩进的多行文本，就地追加到 lines。

    Args:
        error: 待展开的异常；容器型（BaseExceptionGroup）会继续展开其成员。
        lines: 输出缓冲，函数只追加不返回，调用方负责初始化。
        depth: 剩余可展开层数，进入下一层时减 1，归零即截断。
        indent: 当前层的前导缩进（每下一层多两个空格）。

    Returns:
        None（结果通过 lines 就地累积）。

    Events:
        无。

    Raises:
        无（层数由 depth 显式封顶，深链不会触发 RecursionError）。
    """
    # 步骤 1：层数耗尽——留下显式截断标记后返回
    if depth <= 0:
        lines.append(f"{indent}…（更深的异常未展开）")
        return
    # 步骤 2：写当前层「类型: 消息」
    lines.append(f"{indent}{type(error).__name__}: {error}")
    # 步骤 3：容器型展开成员——TaskGroup 产出的 group 其 __cause__/__context__ 均为
    # None，成员不在起因链上，只能从 exceptions 取
    if isinstance(error,BaseExceptionGroup):
        for child in error.exceptions:
            _collect_error_lines(child,lines,depth - 1,indent + "  ")
        return
    # 步骤 4：普通异常沿 __cause__ 继续向内展开
    if error.__cause__ is not None:
        _collect_error_lines(error.__cause__,lines,depth - 1,indent + "  ")


def _format_error_chain(error:BaseException,max_depth:int = ERROR_CHAIN_MAX_DEPTH) -> str:
    """把异常链渲染成文本，供 session 的 reason_text 使用：逐层 `类型: 消息`。

    只走 __cause__（本仓库的错误包装一律是显式 `raise ... from`），超过层数上限显式
    标注截断——截断从外往内，被丢掉的是最内层，故必须留下痕迹而不是静默丢弃。

    Args:
        error: 链头异常（最外层）。
        max_depth: 最大展开层数，超出部分以截断标记代替。

    Returns:
        多行文本；无起因链时只有一行。

    Events:
        无。

    Raises:
        无。
    """
    # 步骤 1：准备输出缓冲
    lines : list[str] = []
    # 步骤 2：递归展开整条异常链
    _collect_error_lines(error,lines,max_depth,"")
    # 步骤 3：拼成最终文本
    return "\n".join(lines)


@dataclass(frozen=True)
class RetryPolicy:
    """一档重试的退避参数：第 n 次重试等待 min(base_delay * multiplier^(n-1), cap) 秒。

    参数值由策略注册表（LLMRerty）以 dict 形式给出，loop 在此承接为本结构再计算退避。

    Attributes:
        base_delay: 第 1 次重试的基准等待秒数。
        multiplier: 每次重试等待的放大倍数。
        cap: 单次等待时长的上限（秒）。

    Events:
        无。

    Raises:
        无。
    """
    base_delay : float = 0.0
    multiplier : float = 1.0
    cap : float = float("inf")


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
        """装配 ReAct 循环：注入依赖、初始化运行态、接线 turn 收尾回调。

        Args:
            event_service: 事件总线，本 loop 的事件出口。
            session: 会话日志，承载全部记录与消息面派生。
            system_prompt: 系统提示词装配/渲染器。
            agent_config: loop 运行参数（step_limit、max_retry_count）。
            llm_client: 模型提供方（流式对话入口）。
            name: agent 名，参与提示词装配；缺省取 uuid 前 8 位。
            prompt_render_parame: 提示词渲染参数，透传给 system_prompt.render。

        Returns:
            None。

        Events:
            不触发事件；仅向 TURN_END 注册回调 tool_register.reset_breaker，使每个
            turn 结束时清空工具熔断状态。

        Raises:
            无（reset_breaker 为绑定方法，满足 register 的 callable 校验）。
        """
        # 步骤 1：标识与依赖注入
        self.name =name  if name else str(uuid.uuid4())[:8]
        self._event_service = event_service
        self.tool_register = ToolRegister()
        self.system_prompt = system_prompt
        self._llm_client = llm_client
        self.agent_config = agent_config
        self.prompt_render_parame = prompt_render_parame
        # 步骤 2：运行态字段（收件箱、任务句柄、状态、超时）
        self.inbox = {"next_turn":[],"next_step":[]}
        self._session = session
        self._task = None
        self.state :Literal["idle","running","maintenance"] = "idle"
        self._LLM_CALL_TIMEOUT = LLM_CALL_TIMEOUT
        # 步骤 3：接线——turn 结束即复位工具熔断
        self._event_service.register(TURN_END.name, self.tool_register.reset_breaker)
        # 步骤 4：唤醒信号，驱动 _loop 消费收件箱
        self._wakeup = asyncio.Event()

    def start(self) -> asyncio.Task:
        """启动后台循环任务（幂等）。

        Args:
            无。

        Returns:
            持有的 _loop 任务；已在运行时复用旧任务而不新建。

        Events:
            无（循环真正取到消息后，由 turn 触发各类事件）。

        Raises:
            无。
        """
        # 步骤 1：已有未结束的任务则直接复用
        if self._task is not None and not self._task.done():
            return self._task
        # 步骤 2：创建循环任务并持有句柄，并接线结束回调（取回异常，防止静默失败）
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_loop_done)
        return self._task

    def cancel(self):
        """取消后台循环并丢弃未消费的消息。

        Args:
            无。

        Returns:
            None。

        Events:
            不直接触发事件；取消以 CancelledError 形式在 _loop / turn / step 中显现，
            由它们记录 interrupted 终态并触发 TURN_END / STEP_END。

        Raises:
            无（CancelledError 在目标任务内部抛出，本函数不抛）。
        """
        # 步骤 1：无任务视为空操作
        if self._task is None:
            return
        # 步骤 2：向任务投递取消
        self._task.cancel()
        # 步骤 3：清空收件箱，避免残留消息在重启后被消费
        self.inbox = {"next_turn":[],"next_step":[]}

    async def _loop ( self ):
        """后台循环：被唤醒后按 next_step、next_turn 顺序逐条消费收件箱。

        Args:
            无。

        Returns:
            None；仅在收到取消时 break 结束协程。

        Events:
            不直接触发事件；每条消息经 turn 触发 TURN_START / TURN_END 及其内部各事件。

        Raises:
            仅吞掉 CancelledError（记一条 warning 后 break）；turn 上抛的其它异常不在此
            捕获，会使循环任务以该异常结束（循环随之停止，需重新 start），该异常由
            start() 接线的 _on_loop_done 取回并记 error 日志。
        """
        while True :
            try :
                # 步骤 1：等待唤醒信号并复位
                await self ._wakeup.wait()
                self ._wakeup.clear()
                # 步骤 2：优先消费 next_step（插队指令），FIFO
                while self .inbox[ "next_step" ]:
                    await self .turn( self .inbox[ "next_step" ].pop( 0 ))
                # 步骤 3：再消费 next_turn（普通轮次），FIFO
                while self .inbox[ "next_turn" ]:
                    await self .turn( self .inbox[ "next_turn" ].pop( 0 ))
            # 步骤 4：被取消则结束循环任务
            except asyncio.CancelledError:
                # 取消 = 结束循环。吞掉取消后协程不会被标记为 cancelled，while 也不会
                # 自己停，所以必须在此显式 break；在途的取消已由 step/turn 记好
                # interrupted 终态，并原样上抛到这里
                logger.warning("agent loop 收到取消：打断循环，任务结束")
                break

    def _on_loop_done(self,task:asyncio.Task):
        """loop 任务结束回调：取回 task 异常，防止静默失败。

        create_task 产出的 task 若无人 await、也无人取异常，异常会被 asyncio 吞掉
        （仅在 task 被 GC 时打印 "Task exception was never retrieved"）；start() 的
        调用方通常也不 await 该 task，本回调是这一异常的唯一出口：

        - 正常结束（含因取消 break）：exc 为 None，无需处理
        - 有异常：记 error 日志（带堆栈），使失败在日志中可见

        注意：取回异常本身也会抑制 asyncio 的 "never retrieved" 警告，因此这里
        必须保证异常不被丢弃。

        Args:
            task: 结束的 _loop 任务。

        Returns:
            None。

        Events:
            无。

        Raises:
            无（task.exception() 在 cancelled 时也会抛，故先判 cancelled）。
        """
        # 步骤 1：被取消的任务没有异常可取
        if task.cancelled():
            return
        # 步骤 2：取异常，正常结束则无事
        exc = task.exception()
        if exc is None:
            return
        # 步骤 3：记 error 日志（带堆栈），让循环的死亡在日志里可见
        logger.error(f"agent loop 任务异常终止：{exc!r}",exc_info=exc)

    def send(self) -> None:
        """唤醒后台循环：按需启动 loop，然后置唤醒信号。**不碰收件箱。**

        这是本 loop 的启动入口与唯一唤醒原语：循环停止（`cancel()` 或异常终止）后
        再次调用会重启一个新的 `_loop` 任务；循环存活时复用旧任务，不重建。

        只负责"把循环叫醒"，消息的入队交给 `followup` / `steer`——它们入队后各自
        调用本方法收尾。故单独调用 `send()` 只是让空闲的循环空转一圈（收件箱为空，
        消费不到东西就又睡下）。

        Args:
            无。

        Returns:
            None。

        Events:
            无（唤醒本身不发事件；turn 内部的 TURN_START 等由 _loop 消费消息时触发）。

        Raises:
            无（`start()` 内部 create_task 需要运行中的事件循环，在无循环的同步上下文
            调用会由 asyncio 抛 RuntimeError）。
        """
        # 步骤 1：循环未启动或已结束则先启动
        if self._task is None or self._task.done():
            self.start()
        # 步骤 2：置唤醒信号，让挂在 wait() 上的 _loop 立刻回到消费循环
        self._wakeup.set()

    @staticmethod
    def _to_user_message(user_prompt : str | list) -> Message:
        """把用户输入归一化为 user 角色的消息（str 包成单个文本块，列表按原样作多模态）。

        Args:
            user_prompt: 字符串或 content 块列表。

        Returns:
            可直接入队的 Message。

        Events:
            无。

        Raises:
            无。
        """
        content = [{"type":"text","text":user_prompt}] if isinstance(user_prompt,str) else user_prompt
        return Message(role= "user",content=content)

    def followup(self,user_prompt : str | list) -> None:
        """追加一条用户消息到队尾（本轮之后排队），并入队后唤醒循环。

        语义是"跟在后面"：已有的排队消息（含此前 steer 的插队消息）都先于它被消费。
        结尾自动调用 `send()`，故调用方无需再自己唤醒。

        Args:
            user_prompt: 字符串按单个文本块包装；列表按原样作为多模态 content。

        Returns:
            None。

        Events:
            无（只入队并唤醒；TURN_START 等在 _loop 消费到该消息时触发）。

        Raises:
            无。
        """
        # 步骤 1：入队——追加到队尾，保持先来先服务
        self.inbox["next_turn"].append(self._to_user_message(user_prompt))
        # 步骤 2：唤醒循环消费
        self.send()

    def steer(self,user_prompt : str | list) -> None:
        """把一条用户消息插到队首（插队到下一条），并入队后唤醒循环。

        与 `followup` 的唯一差别是落点：本方法插到 `next_turn` 第 0 位，故它先于
        所有已排队的消息被消费。每次都插到第 0 位，因此连续 steer 是"后插先跑"。

        **不打断在途的 turn**：正在跑的那一轮照常收尾，插队消息在它结束后的下一轮
        才被消费；要立刻中断请用 `cancel()`（那会连同收件箱一起作废）。

        理想用法：发生某些错误之后，需要向AI给出额外的提示，比如输出内容过长，出现截断错误，
        这时候打断当前轮，然后给出提示

        Args:
            user_prompt: 字符串按单个文本块包装；列表按原样作为多模态 content。

        Returns:
            None。

        Events:
            无（只入队并唤醒；TURN_START 等在 _loop 消费到该消息时触发）。

        Raises:
            无。
        """
        # 步骤 1：入队——插到队首
        self.inbox["next_turn"].insert(0,self._to_user_message(user_prompt))
        # 步骤 2：唤醒循环消费
        self.send()

    def persist_session_now(self):
        """立即把 session 落盘（磁盘错误静默，不影响主流程）。

        Args:
            无。

        Returns:
            None。

        Events:
            无。

        Raises:
            OSError: 被就地吞掉，不外抛。
            其它异常: persistence.presist() 抛出的非 OSError 原样上抛。
        """
        # 步骤 1：尝试落盘，磁盘错误静默跳过
        try:
            self._session.presistence.presist()
        except OSError:
            pass

    async def turn(self,user_message):
        """完成一轮：写 turn 边界、驱动 step 循环、汇总并落定终态。

        Args:
            user_message: 本轮的首条用户消息。

        Returns:
            None。

        Events:
            触发 TURN_START 与 TURN_END；step 内部另有 STEP_START / STEP_END、
            USER_MESSAGE、REQUEST_HEADER、TOOL_CALL / TOOL_RESULT、
            ASSISTANT_CHUNK / ASSISTANT_MESSAGE、REQUEST_ERROR（waterfall）。
            TURN_END 同时是工具熔断复位的触发点（见 __init__ 的接线）。

        Raises:
            asyncio.CancelledError: 记 interrupted 后原样上抛。
            Exception: step 抛出的任何异常，记 error + 异常链后原样上抛；
                session.append 的入参校验异常（ValueError / TypeError）同样直接冒出。
        """
        # 步骤 1：预置终态，默认为本轮成功
        final_result = FinalResult(reason_type="success",reason_text="",error_type="")
        try:
            # 步骤 2：写 turn/start 记录并广播
            self._session.append("turn/start",TurnStartData())
            self._event_service.emit(TURN_START.name,TurnStartPayload())
            # 步骤 3：进入 step 循环（重试与终止由 step 内部决策）
            await self.step(user_message)
        # 步骤 4：取消——记 interrupted 后原样上抛
        except asyncio.CancelledError as e:
            final_result.reason_type = "interrupted"
            final_result.reason_text = "用户主动打断"
            final_result.error_type = type(e).__name__
            raise
        # 步骤 5：其它异常——记 error + 异常链后原样上抛
        except Exception as e:
            final_result.reason_type = "error"
            final_result.reason_text = _format_error_chain(e)
            final_result.error_type = type(e).__name__
            raise
        finally:
            # 步骤 6：无论成败都写 turn/end 并广播（一并触发工具熔断复位）
            self._session.append("turn/end",TurnEndData(reason_type=final_result.reason_type,reason_text=final_result.reason_text,error_type=final_result.error_type))
            self._event_service.emit(TURN_END.name,TurnEndPayload())

    def _request_header(self,user_message : Message | None) -> list[Message]:
        """装配本次请求的消息面：system、system-reminder、运行时上下文与会话派生历史。

        Args:
            user_message: 本轮用户消息；None 表示重试续跑（消息已在会话里，不再注入）。

        Returns:
            可直接发给模型的完整消息列表。

        Events:
            有 user_message 时触发 USER_MESSAGE；首次请求或请求头发生变化
            （模型名 / 系统提示词 / 提醒 / 工具 schema / 参数任一不同）时触发
            REQUEST_HEADER。写 user/message、request/header 记录时另触发 session/event。

        Raises:
            ValueError / TypeError: session.append 的入参校验失败（类型不在记录表内）。
        """
        # 步骤 1：装配系统提示词与运行时提醒（每次请求都重新渲染）
        assembly_prompt : AssemblyPrompt = self.system_prompt.assemble(self.name)
        system_prompt = self.system_prompt.render(assembly_prompt,self.prompt_render_parame)
        system_reminder = assembly_prompt.system_reminder

        # 步骤 2：首步才注入用户消息，并把运行时上下文合并进该消息
        if user_message:
            merged = self._merge_runtime_context(user_message,assembly_prompt.context)
            self._session.append("user/message",UserMessageData(merged),surface_op="append")
            self._event_service.emit(USER_MESSAGE.name,UserMessagePayload())

        # 步骤 3：算出本次请求头快照
        current = RequestHeaderData(
            reason="initial",
            model_name=self._llm_client.model,
            system_prompt=system_prompt,
            system_reminder=system_reminder,
            tools=self.tool_register.to_schemas(),
            params=self._llm_client.params,
        )
        # 步骤 4：与上一条快照比对，仅首次或发生变化时才记录并广播
        last = self._session.latest_request_header()
        if last is None:
            self._session.append("request/header", current)
            self._event_service.emit(REQUEST_HEADER.name,RequestHeaderPayload())
        elif (last.model_name, last.system_prompt, last.system_reminder, last.tools, last.params) != (current.model_name, current.system_prompt, current.system_reminder, current.tools, current.params):
            current.reason = "change"
            self._session.append("request/header", current)
            self._event_service.emit(REQUEST_HEADER.name,RequestHeaderPayload())

        # 步骤 5：拼消息面——system → system-reminder → 会话派生的历史消息
        messages : list[Message] = []
        if system_prompt:
            messages.append(Message(role="system",content=system_prompt))
        if system_reminder:
            messages.append(Message(role="user",content=f"<system-reminder>{system_reminder}</system-reminder>"))
        messages.extend(self._session.derive_messages())
        return messages

    @staticmethod
    def _merge_runtime_context(message : Message,runtime_context : str) -> Message:
        """把运行时上下文追加到用户消息内容末尾，返回新消息（不改原对象）。

        Args:
            message: 原始用户消息。
            runtime_context: 运行时上下文文本；为空时原样返回 message。

        Returns:
            合并后的 Message；上下文非空时 content 为 [原内容, 上下文] 块列表。

        Events:
            无。

        Raises:
            无（str 形式的 content 会先归一化为块列表）。
        """
        # 步骤 1：无上下文则不做任何包装
        if not runtime_context:
            return message
        # 步骤 2：content 归一化为可变块列表，再追加上下文块
        content = [{"type":"text","text":message.content}] if isinstance(message.content,str) else list(message.content)
        content.append({"type":"text","text":runtime_context})
        # 步骤 3：复制其余字段，返回新消息
        return Message(
            role=message.role,
            content=content,
            tool_calls=message.tool_calls,
            tool_call_id=message.tool_call_id,
            reasoning_content=message.reasoning_content,
        )

    def _effective_step_limit(self) -> int:
        """本 turn 的步数上限 = 配置基准 + 本 turn session里授予的额外步数。

        预算不持存在 loop 的字段里，每次判上限时现算：授予由 hand_decision 落成
        agent/grant 记录（见 _record_agent_grant），故崩溃恢复后重算结果不变。
        """
        return self.agent_config.step_limit + self._session.granted_steps(self._session.turn)

    async def step(self,user_message):
        """驱动一次「模型 → 工具 → 再模型」的循环，直到无工具调用或本轮被终止。

        错误处理统一收拢在 finally 内，按三个阶段依次执行：

        1. session 信息补全：异常（如取消）打断工具调用时，补齐缺失的 tool/result
        2. 记账：写 step/end，带本轮结果（无错 success / 取消 interrupted / 其它 error + 异常链）
        3. 错误处理：交 _handle_error 决策——取消与无人认领原样上抛，retry 类退避后继续，
           其余决策 break（break 的语义未定且当前不可达，见 docs/known-limitations）

        step/end 只在"这一步真的开始过"（step_opened）时写：步数预算在开始前就拒绝的
        那一轮没有 step/start，不应留下无配对的 step/end。

        Args:
            user_message: 本轮首步的用户消息；首步之后置 None，后续步只带会话增量。

        Returns:
            None；模型不再请求工具、或被终止决策打断时返回。

        Events:
            直接触发 STEP_START / STEP_END、TOOL_CALL / TOOL_RESULT；经 _request_header
            触发 USER_MESSAGE / REQUEST_HEADER，经 _ask_model 触发 ASSISTANT_CHUNK /
            ASSISTANT_MESSAGE，出错时经 _handle_error 触发 REQUEST_ERROR（waterfall）。
            每条 session.append 另会触发 session/event。

        Raises:
            MaxStepsExceededError: 步数达到本 turn 的有效上限（配置基准 + session授予合计）。
            AgentUnclaimedError: request/error 无人认领该错误。
            RetryExhaustedError: 重试次数超过 agent_config.max_retry_count。
            asyncio.CancelledError: 取消，原样上抛。
            Exception: _ask_model / 工具执行 / session.append 抛出的其它异常原样上抛；
                同批多个工具异常由 TaskGroup 包成 ExceptionGroup 抛出。
        """
        while True:
            step_error = None
            step_opened = False

            try:
                # 步骤 1：步数预算检查——开始前拒绝，故不会产生 step/end 配对问题。
                # 判据现算自session，loop 不持存步数：已走步数即 session.step，
                # 上限是配置基准 + 本 turn 授予合计
                effective_limit = self._effective_step_limit()
                if self._session.step >= effective_limit:
                    raise MaxStepsExceededError(f"达到最大步数{effective_limit}，强制终止本turn")
                # 步骤 2：开步——写 step/start、广播（session.step 随之自增）
                self._session.append("step/start",StepStartData())
                self._event_service.emit(STEP_START.name,StepStartPayload())
                step_opened = True
                # 步骤 3：装配消息面（用户消息只在首步注入）并落盘
                messages = self._request_header(user_message)
                user_message = None
                self.persist_session_now()
                # 步骤 4：调用模型并落盘结果，整体受 _LLM_CALL_TIMEOUT 约束
                response = await asyncio.wait_for(self._ask_model(messages),self._LLM_CALL_TIMEOUT)

                self.persist_session_now()
                # 步骤 5：有工具调用则执行后进入下一步，否则收敛结束
                if response.tool_call_requests:
                    await self._tool_call(response)
                else:
                    break


            # 步骤 6：取消与普通异常统一记为 step_error，交给 finally 处置
            except asyncio.CancelledError as e:
                step_error = e
            except Exception as e:
                step_error = e
            finally:
                # 阶段 1：session 信息补全
                self._complete_session(step_error)
                # 阶段 2：记账
                if step_opened:
                    reason_type,reason_text = self._step_end_reason(step_error)
                    self._session.append("step/end",StepEndData(reason_type=reason_type,reason_text=reason_text))
                    self._event_service.emit(STEP_END.name,StepEndPayload())
                # 阶段 3：错误处理——决策已归一为控制信号（见 LOOP_CONTROL 词表）；
                # 只有 break 结束本 turn，continue 回到循环顶部走下一轮
                decision = await self._handle_error(step_error)
                if decision != "continue":
                    break

    async def _tool_call(self,response:LLMResponse):
        # 步骤 5a：并发执行本批工具，先逐条登记 tool/call 并广播
        async with asyncio.TaskGroup() as tg:
            tasks = []
            #tool_call_decision {call_id: {"decision": "deny", "reason": str}}，可能为空表。只装deny的
            tool_call_decision:dict  = await self._event_service.waterfall(TOOL_CALL.name,ToolCallPayload(response.tool_call_requests,self._session)) or {}
            for tool_call in response.tool_call_requests:
                permission_passed = True
                deny_reason = ""
                if tool_call.id in tool_call_decision:
                    permission_passed = False
                    deny_reason = tool_call_decision[tool_call.id]["reason"]
                self._session.append("tool/call",
                    ToolCallData(
                        tool_name=tool_call.name,
                        call_id = tool_call.id ,
                        arguments=tool_call.arguments,
                        permission_passed=permission_passed,
                        deny_reason=deny_reason)
                    )
                tasks.append(tg.create_task(self.tool_register.execute(tool_call,permission_passed,deny_reason)))
                            
        # 步骤 5b：按原调用顺序回收结果，写 tool/result 并广播
        # 必须在 TaskGroup 之外：组内 task 要到 with 退出时才被 await，组内取
        # task.result() 只会拿到 InvalidStateError（Result is not set）
        for index,task in enumerate(tasks):
            tool_call = response.tool_call_requests[index]
            tool_result =  task.result()
            self._session.append("tool/result",ToolResultData(call_id=tool_call.id,tool_name=tool_call.name,message=Message(role="tool",content=tool_result.content,tool_call_id=tool_call.id,),is_error=tool_result.is_error,duration_ms=tool_result.duration_ms),surface_op="append",source_event_seqs=[])
            self._event_service.emit(TOOL_RESULT.name,ToolResultPayload(
                tool_name=tool_call.name,
                arguments=tool_call.raw_arguments,
                is_error=tool_result.is_error,
                error_type=tool_result.error_type.value if tool_result.error_type else None,
                content=tool_result.content,
                duration_ms=tool_result.duration_ms,
            ))
    @staticmethod
    def _step_end_reason(step_error) -> tuple[str,str]:
        """本轮的记账内容：无错 success；取消 interrupted；其余 error + 异常链文本。

        Args:
            step_error: 本步捕获到的异常；None 表示本步无错。

        Returns:
            (reason_type, reason_text) 二元组，供写 step/end 使用。

        Events:
            无。

        Raises:
            无。
        """
        # 步骤 1：无错记为 success
        if step_error is None:
            return "success",""
        # 步骤 2：取消记为 interrupted
        if isinstance(step_error,asyncio.CancelledError):
            return "interrupted","用户主动打断"
        # 步骤 3：其余记为 error，附异常链文本
        return "error",_format_error_chain(step_error)

    async def _ask_model(self,messages : list[Message])->LLMResponse:
        """消费一次模型的流式输出，边收边落库。

        Args:
            messages: 完整请求消息面（system + 历史）。

        Returns:
            流末的 LLMResponse（承载 content / tool_calls / reasoning / usage）。

        Events:
            每个增量触发 ASSISTANT_CHUNK（写 assistant/chunk），终态响应触发
            ASSISTANT_MESSAGE（写 assistant/message）；均另触发 session/event。

        Raises:
            LLMCallError: 模型层领域异常，原样上抛交由 step 决策。
            Exception: provider 层其它异常原样上抛。
            UnboundLocalError: 流未产出任何 LLMResponse 时，返回处的 response 未绑定。
        """
        try:
            # 步骤 1：逐块消费模型流（工具 schema 每次现取，含熔断过滤）
            async for item in self._llm_client.stream_chat(messages,self.tool_register.to_schemas()):
                # 步骤 2a：终态响应——写 assistant/message 并广播
                if isinstance(item, LLMResponse):
                    response = item
                    self._session.append("assistant/message",AssistantMessageData(Message(
                        role = "assistant",
                        content = response.content,
                        tool_calls=response.tool_call_requests,
                        reasoning_content=response.reasoning_content,
                    ),usage=response.usage),surface_op="append",source_event_seqs=[])
                    self._event_service.emit(ASSISTANT_MESSAGE.name,AssistantMessagePayload())
                # 步骤 2b：流式增量——写 assistant/chunk 并广播
                else:
                    self._session.append("assistant/chunk",AssistantChunkData(item))
                    self._event_service.emit(ASSISTANT_CHUNK.name,AssistantChunkPayload())
        # 步骤 3：模型层错误原样上抛（此处不补记，交由 step 决策）
        except LLMCallError:
            raise

        # 步骤 4：返回流末响应
        return response

    def _complete_session(self,step_error):
        """补齐因异常（如取消）打断工具调用而缺失的 tool/result。

        缺失配对会让消息面出现"有 tool_calls 却没有 tool 响应"的非法组合，续跑时会被
        模型接口拒绝，故补一条占位结果（is_error=True）。
        这里之后需要重做

        Args:
            step_error: 本步异常；None 表示无错，直接返回。

        Returns:
            None。

        Events:
            每补一条占位结果触发 TOOL_RESULT，并写对应 tool/result 记录
            （另触发 session/event）。

        Raises:
            ValueError / TypeError: session.append 的入参校验失败。
        """
        # 步骤 1：无异常则无需补齐
        if step_error is None:
            return
        # 步骤 2：先收集已有应答的 call_id，避免重复补
        answered = {r.data.call_id for r in self._session.record_list if r.type == "tool/result"}
        for record in self._session.record_list:
            # 步骤 3：只处理尚未应答的 tool/call
            if record.type != "tool/call" or record.data.call_id in answered:
                continue
            call = record.data
            content = "工具调用被中断，未产生结果"
            # 步骤 4：补一条占位 tool/result（is_error=True）并广播
            self._session.append("tool/result",ToolResultData(
                call_id=call.call_id,
                tool_name=call.tool_name,
                message=Message(role="tool",content=content,tool_call_id=call.call_id),
                is_error=True,
            ),surface_op="append",source_event_seqs=[])
            self._event_service.emit(TOOL_RESULT.name,ToolResultPayload(
                tool_name=call.tool_name,
                arguments=call.arguments,
                is_error=True,
                error_type=None,
                content=content,
                duration_ms=None,
            ))

    async def _handle_error(self,step_error):
        """本轮的处置决策：返回决策值供循环判断是否继续；None 表示本轮无错误。

        - 取消：原样上抛（由 _loop / 上层收尾），不进入决策链
        - 无决策（无人认领）：抛 AgentUnclaimedError（from 原错误）
        - retry / backoff_retry：按策略退避等待后返回决策（循环继续）
        - 重试次数耗尽：抛 RetryExhaustedError（from 最后一次错误）
        - 其它决策：原样返回（循环走 break；语义未定，见 docs/known-limitations）

        只有"决定重试"的那次在发生点落一条 llm/retry：它既是"第几次重试"的事实，也是
        下轮 attempt 的计数来源（llm_retry_count 按该记录条数统计）。无人认领与耗尽都不
        重试，故不落该记录——它们的错误信息由 step/end 与 turn/end 的 reason_text 承载。

        Args:
            step_error: 本步异常；None 表示本轮无错。

        Returns:
            None（无错），或决策值（retry / backoff_retry / 其它）供调用方判断是否继续。

        Events:
            触发 REQUEST_ERROR（waterfall，向策略注册表取 decision / policy）；
            决定重试时经 _record_llm_retry 写 llm/retry 记录（另触发 session/event）。

        Raises:
            asyncio.CancelledError: 原样上抛，不进入决策链。
            AgentUnclaimedError: request/error 返回的 decision 为 None（无人认领）。
            RetryExhaustedError: attempt 超过 agent_config.max_retry_count。
        """
        # 步骤 1：无错直接返回 None
        if step_error is None:
            return None
        # 步骤 2：取消不进决策链，原样上抛
        if isinstance(step_error,asyncio.CancelledError):
            raise step_error
        # 步骤 3：触发 request/error waterfall，取裁决值。返回值是无类型 dict，key 名
        # 只在本点出现——在此拆成具名参数，下游不必再知道 dict 里有什么
        verdict = await self._event_service.waterfall(REQUEST_ERROR.name,RequestErrorPayLoad(error_type=step_error)) or {}
        # 步骤 4：集中处理 decision，最终返回 continue or break
        return await self.hand_decision(
            step_error,
            decision = verdict.get("decision"),
            policy   = verdict.get("policy"),
            grant    = verdict.get("grant"),
        )

    async def hand_decision(self,step_error, decision, policy=None, grant=None) -> str:
        """把 waterfall 的裁决落定成循环控制信号（取值见 LOOP_CONTROL）。

        裁决携带的状态变更在此落地，而不是由订阅方直接改 loop 的状态：执行权留在
        loop（只有它知道自己的运行态），订阅方只表达意图——grant 是"申请放宽 N 步
        预算"，由本方法落成 agent/grant session记录，下轮判预算时现算（见
        _effective_step_limit）。故 loop 不持存预算，也无需按错误类型二次判别。

        Args:
            step_error: 触发本次裁决的异常，用于记账与耗尽时的 from 链。
            decision: waterfall 的决策值；None 表示无人认领。
            policy: 退避参数（retry / backoff_retry 档消费）。
            grant: 预算授予意图，形如 {"steps": N}；仅 continue 档消费。

        Returns:
            "continue"（走下一轮）或 "break"（结束本 turn）。

        Events:
            重试档经 _record_llm_retry 写 llm/retry；继续档带 grant 时经
            _record_agent_grant 写 agent/grant（两者均另触发 session/event）。

        Raises:
            AgentUnclaimedError: decision 为 None，没有任何订阅方认领该错误。
            RetryExhaustedError: 重试次数超过 agent_config.max_retry_count。
        """
        # 步骤 1：无人认领——直接抛 AgentUnclaimedError（未重试，不落 llm/retry）
        if decision is None:
            raise AgentUnclaimedError("无错误处理策略的错误") from step_error
        # 步骤 2：重试档——算序号、超限即抛、记账、按策略退避，然后继续
        if decision in RETRY_DECISIONS:
            attempt = self._session.llm_retry_count(self._session.turn) + 1
            max_retry_count = self.agent_config.max_retry_count
            if attempt > max_retry_count:
                raise RetryExhaustedError(f"{max_retry_count}/{max_retry_count} 达到最大重试错误") from step_error
            self._record_llm_retry(step_error,decision,attempt)
            await self.retry_delay(step_error,RetryPolicy(**(policy or {})),attempt)
            return "continue"
        # 步骤 3：继续档——把授予的预算落成session事实，再继续
        if decision == "continue":
            if grant:
                self._record_agent_grant(step_error,decision,grant)
            return "continue"
        # 步骤 4：其余（break）原样作为控制信号
        return decision


    def _record_llm_retry(self,error,decision:str,attempt:int) -> None:
        """落一条 llm/retry 记录：第几次重试、因何决策、触发它的错误本身。

        只在真正决定重试时调用——该记录的条数是本 turn 重试次数的唯一口径
        （`Session.llm_retry_count`），非重试的失败不得写入。

        Args:
            error: 触发本次重试的异常，用于记录类型与异常链文本。
            decision: 触发本次重试的决策，取值 retry / backoff_retry。
            attempt: 本次重试序号，从 1 开始。

        Returns:
            None。

        Events:
            写 llm/retry 记录（经 session 触发 session/event）。

        Raises:
            ValueError / TypeError: session.append 的入参校验失败。
        """
        # 步骤 1：把本次重试落成一条可结构化查询的 llm/retry 记录
        self._session.append("llm/retry",LLMRetryData(
            retry_count=attempt,
            reason=decision,
            error_type=type(error).__name__,
            error_message=_format_error_chain(error),
        ))

    def _record_agent_grant(self,error,decision:str,grant:dict) -> None:
        """落一条 agent/grant：本次授予多少步、因何决策、触发它的错误。

        只在真正放宽预算时调用——该类型记录的 steps 合计是后续每轮判预算的依据
        （`Session.granted_steps`），故 break / 无人认领这类"未授予"的收尾不得写入。
        与 _record_llm_retry 对仗：发生点记账，session即事实。

        Args:
            error: 触发本次授予的异常，用于记录错误类名。
            decision: 触发本次授予的决策（continue）。
            grant: 授予意图，形如 {"steps": N}。

        Returns:
            None。

        Events:
            写 agent/grant 记录（经 session 触发 session/event）。

        Raises:
            KeyError: grant 缺 steps 字段——决策方给的形状不对，不静默按 0 记账。
        """
        # 步骤 1：把本次授予落成一条可结构化查询的记录
        self._session.append("agent/grant",AgentGrantData(
            steps=grant["steps"],
            reason=decision,
            error_type=type(error).__name__,
        ))

    async def retry_delay(self,error,policy:RetryPolicy,attempt:int)->float:
        """计算并等待第 attempt 次重试的退避时长，返回实际等待秒数。

        delay = min(base_delay * multiplier^(attempt-1), cap)；错误携带服务端
        Retry-After 建议时取其与本地退避的较大值（服务端更清楚何时可用，本地退避
        作为下限）。非领域异常（如 TimeoutError）没有 details，仅用本地退避。

        Args:
            error: 本次失败的异常，尝试从 details["retry_after"] 读服务端建议。
            policy: 本档退避参数（base_delay / multiplier / cap）。
            attempt: 重试序号，从 1 开始，决定指数放大的次数。

        Returns:
            实际等待的秒数（0 表示未等待）。

        Events:
            无。

        Raises:
            asyncio.CancelledError: 等待期间被取消时上抛。
        """
        # 步骤 1：按指数退避公式算本地等待时长（cap 封顶）
        delay = min(policy.base_delay * policy.multiplier ** (attempt - 1),policy.cap)
        # 步骤 2：取服务端 Retry-After 建议，作为等待下限
        retry_after = (getattr(error,"details",None) or {}).get("retry_after")
        if isinstance(retry_after,(int,float)):
            delay = max(delay,retry_after)
        # 步骤 3：需要等待才 sleep，并返回实际等待秒数
        if delay > 0:
            await asyncio.sleep(delay)
        return delay
