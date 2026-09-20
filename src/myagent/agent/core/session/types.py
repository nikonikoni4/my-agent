"""session 记录的 data 格式定义。

每种记录类型（信封的 type 标签）对应一个 Data 数据类，约束该记录 data 字段的结构。
信封（SessionRecord）与 Session 类见 session.py；本模块只定义格式，不含读写逻辑。

标签命名沿用两层词汇隔离（参照 DeepSeek-Harness）：
- 带斜杠：会话事件（turn/start、tool/call 等），事实发生时写入
- 不带斜杠：打包存储行（content-chunks 等），多个流式片段攒成一条记录

turn/step 是记录在时间轴上的定位信息，统一放在信封 SessionRecordData 上，data 只描述事件内容本身。
"""

from dataclasses import dataclass, field, asdict
from typing import Literal
from myagent.agent.core.provider import ChatParams, Message, Usage, StreamChunk
import datetime,uuid


class SessionData:
    """所有 data 类型的基类：统一类型标注，提供存储格式转换入口。

    内存中 data 字段保存类型实例，只有持久化组件在写文件时才转成 dict。
    有特殊校验或转换需求的类覆写 to_record_dict。
    """
    def to_record_dict(self) -> dict:
        return asdict(self)


@dataclass
class TurnStartData(SessionData):
    """turn/start：一轮开始。一轮 = 用户一条消息到助手完整答完。"""


@dataclass
class StepStartData(SessionData):
    """step/start：一个步骤开始。一步 = 一次 LLM 调用 + 执行其请求的工具。"""


@dataclass
class RequestHeaderData(SessionData):
    """request/header：一次 LLM 请求的配置快照，仅首次和配置变更时写入。

    system_prompt / system_reminder 是动态编排的两段请求前缀（分别位于 Message List
    第 1、2 位），不落 session 的 message list，由本事件承载以保证每一步输入可复现。
    """
    reason: Literal["initial", "resume", "change"]
    model_name: str
    system_prompt: str
    tools: list[dict]  # 工具 schema 列表（Tool.to_schema() 的输出）
    params: ChatParams | None  # 采样参数，None 表示全部用供应商默认值
    system_reminder: str = ""  # System Reminder（第 2 位消息），空表示无


@dataclass
class UserMessageData(SessionData):
    """user/message：一条进入对话的用户消息。"""
    message: Message


@dataclass
class ContentChunksData(SessionData):
    """content-chunks：正文流式片段的打包存储行。"""
    dt: list[int]  # 相邻片段的毫秒间隔，长度 = len(texts) - 1
    texts: list[str]  # 逐片段保存不拼接，token 边界是数据


@dataclass
class ReasoningChunksData(SessionData):
    """reasoning-chunks：推理过程流式片段的打包存储行。字段含义同 ContentChunksData。"""
    dt: list[int]
    texts: list[str]


@dataclass
class ToolCallChunksData(SessionData):
    """tool-call-chunks：一次工具调用的参数流碎片打包行。"""
    index: int  # 槽位号，并行调用时区分归属
    id: str  # 调用标识，整个调用期间恒定，只存一次
    name: str  # 工具名，同上
    dt: list[int]  # 长度 = len(args) - 1
    args: list[str]  # 参数 JSON 碎片逐片保存，单个碎片不是合法 JSON

@dataclass(init=False)
class AssistantChunkData(SessionData):
    """assistant/chunk：助手流式增量片段的内存事件记录，持久化时才打包为 content-chunks 等存储行。

    type 标注片段类型（对齐 harness 用 chunk 判别字段区分的方式），打包时据此分流到对应存储行。
    一次对应一个片段，字段按类型部分填充，其余为 None：
    content/reasoning 片段只填 texts；tool-call 片段只填 index/id/name/args；
    finish 片段只填 finish_reason（结束原因与文本增量语义不同，单独承载）。
    """
    type: Literal["content", "reasoning", "tool-call", "finish"] | None = None  # 片段类型，打包时映射到 content-chunks / reasoning-chunks / tool-call-chunks / finish-chunks
    index: int | None = None  # 槽位号，并行调用时区分归属；非工具片段为 None
    id: str | None = None  # 调用标识，仅每个调用的首个片段携带，后续片段为 None
    name: str | None = None  # 工具名，携带规则同 id
    args: str | None = None  # 本片段的参数 JSON 碎片，单个碎片不是合法 JSON；非工具片段为 None
    texts: str | None = None  # 本片段的文本增量；工具调用、finish 片段为 None
    finish_reason: str | None = None  # 结束原因（stop/length/...），仅 finish 片段填充；其余片段为 None

    def __init__(self, chunk: StreamChunk):
        # provider 流每个片段只填一类字段（见 openai_provider.stream_chat），按字段推断类型
        if chunk.tool_index is not None:
            self.type = "tool-call"
            self.index = chunk.tool_index
            self.id = chunk.tool_id
            self.name = chunk.tool_name
            self.args = chunk.tool_arguments_delta
        elif chunk.content is not None:
            self.type = "content"
            self.texts = chunk.content
        elif chunk.reasoning_content is not None:
            self.type = "reasoning"
            self.texts = chunk.reasoning_content
        elif chunk.finish_reason is not None:
            self.type = "finish"
            self.finish_reason = chunk.finish_reason
        else:
            self.type = None


@dataclass
class AssistantMessageData(SessionData):
    """assistant/message：一次 LLM 调用的完整结果。"""
    message: Message
    usage: Usage = field(default_factory=Usage)

@dataclass
class ToolCallData(SessionData):
    """tool/call：一次工具调用，执行时写入、先于 tool/result。"""
    call_id: str  # 与 tool/result 配对；并行调用同一工具时靠它区分
    tool_name: str
    # 与 RawToolCall.arguments 同两形态：provider 解析成功为 dict，未解析成功即
    # 模型当时写的 wire 原文 str。落盘按拿到时的形态原样存（str 存 JSON string、
    # dict 存嵌套对象），不在这里归一化——由各读回侧自己决定要哪种形态
    # （报表侧见 stats/session_view.py 的 _wire_arguments）
    arguments: str | dict
    permission_passed : bool = True
    deny_reason : str = ""

@dataclass
class ToolResultData(SessionData):
    """tool/result：一次工具执行的结果。"""
    call_id: str  # 与 tool/call 配对
    tool_name: str
    message: Message  # role="tool"、tool_call_id=call_id 的结果消息
    duration_ms: int | None = None
    is_error : bool = False 

@dataclass
class StepEndData(SessionData):
    """step/end：一个步骤结束。"""
    reason_type: Literal["success", "interrupted","error"]
    reason_text : str 

@dataclass
class TurnEndData(SessionData):
    """turn/end：一轮结束。"""
    reason_type: Literal["success", "interrupted","error"]
    reason_text : str 
    error_type : str

@dataclass
class CompactionStartData(SessionData):
    """compaction/start：压缩事务开始。"""
    compaction_id: str


@dataclass
class CompactionSummaryData(SessionData):
    """compaction/summary：压缩结果的事实记录。"""
    compaction_id: str
    summary: str  # 摘要正文
    shadowed_seqs: list[int]  # 被遮蔽的消息记录 seq 清单，权威清单
    shadowed_token_count: int  # 被遮蔽内容的 token 估价
    model_name: str  # 生成摘要的模型
    usage: Usage


@dataclass
class CompactionEndData(SessionData):
    """compaction/end：压缩事务结束。"""
    compaction_id: str


@dataclass
class SessionMetaData:
    cwd : str
    session_id : str = field(default_factory=lambda : str(uuid.uuid4()))
    name : str = ""  # 为空时在 __post_init__ 中取 session_id
    created_at : datetime.datetime = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at : datetime.datetime | None = None
    format_version : int = 1
    parent_session_id : str | None = None
    def __post_init__(self):
        """name 为空时回退为 session_id，保证每个会话总有可读的名称。"""
        if not self.name:
            self.name = self.session_id

    def meta_data(self,updated_at:datetime.datetime |None = None)->dict:
        """生成会话的元信息 dict（落盘文件第一行的内容），时间是唯一的 isoformat 转换点。

        Args:
            updated_at: 显式指定的更新时间；不传时优先用实例的 updated_at，
                实例也为空则取当前 UTC 时间。

        Returns:
            含 type/session_id/name/last_compact_loc/created_at/updated_at 的 dict。
        """
        if updated_at is None:
            updated_at = self.updated_at if self.updated_at else datetime.datetime.now(datetime.timezone.utc)
        return {
            "type":"meta_data",
            "format_version" : self.format_version,
            "session_id":self.session_id,
            "cwd" : self.cwd,
            "name" : self.name,
            "created_at":self.created_at.isoformat(),
            "updated_at":updated_at.isoformat(),
            "parent_session_id" : self.parent_session_id,
        }
@dataclass
class SessionRecordData:
    # 字段顺序即 jsonl 落盘键序：定位字段（turn/step/surface_op/source_event_seqs）
    # 排在 data 前面，人工查看 session 文件时先读定位信息再看载荷
    type : str # event的类型
    seq : int # session jsonl的顺序，从非metadata的数据开始
    turn : int | None = None # 定位：事件发生在第几轮；0 表示第一条 turn/start 之前，轮间压缩为 None
    step : int | None = None # 定位：事件发生在本轮第几步；0 表示本轮第一个 step/start 之前，不属于任何 step 的事件为 None
    surface_op : str | dict = None
    data : SessionData = field(kw_only=True) # RECORD_DATA_TYPES 中的类型实例，落盘时由持久化组件经 to_record_dict 转为 dict
    source_event_seqs : list | None =None # 当surface_op 是{op : replace ,start,end}时必须要
    timestamp : datetime.datetime = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    uuid : str = field(default_factory=lambda : str(uuid.uuid4()))
    
    def to_record_dict(self)->dict:
        """"""
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d

@dataclass
class TextChunkData:
    """text-chunk 打包存储行的 data。

    片段身份是位置性的（参照 DeepSeek-Harness 的 chunk-rows）：第 k 个成员的
    seq = 信封 seq（组首）按 source_event_seqs 逐位还原，不存每片段 uuid。
    """
    type : Literal["content","reasoning","tool-call","finish"]
    first_seq : int
    index : int 
    id : str | None 
    name : str | None
    args : list[str] | None
    texts : list[str] | None
    finish_reason : list[str] | None  # finish 行的结束原因逐片对齐；非 finish 行为 None
    dt : list[int]
    
@dataclass
class LLMRetryData(SessionData):
    """llm/retry：一次重试的记录（第几次重试、因何决策、触发它的错误）。

    只在真正决定重试时写入：`Session.llm_retry_count` 按本类型记录的条数统计重试
    次数，故无人认领 / 重试耗尽这类"未重试"的收尾不得写入（它们的错误信息由
    step/end 与 turn/end 的 reason_text 承载）。
    """
    retry_count : int # 第 n 次重试，从 1 开始
    reason : str # 触发本次重试的决策（retry / backoff_retry）
    error_type : str = "" # 触发本次重试的错误类名
    error_message : str = "" # 触发本次重试的错误信息（异常链文本）


@dataclass
class AgentGrantData(SessionData):
    """agent/grant：一次预算授予的记录（授予多少、因何决策、触发它的错误）。

    只在真正决定"继续并放宽预算"时写入：`Session.granted_steps` 按本类型记录的
    steps 求和，得该 turn 的额外步数预算，故 break / 无人认领这类"未授予"的收尾
    不得写入。

    授予是**账本事实**而非 loop 的持存状态：决策方（如人在回路）只表达意图，
    由 loop 落成本记录，判预算时再读回来现算。
    """
    steps : int # 本次授予的额外步数
    reason : str # 触发本次授予的决策（continue）
    error_type : str = "" # 触发本次授予的错误类名