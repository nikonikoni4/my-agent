"""session 记录的 data 格式定义。

每种记录类型（信封的 type 标签）对应一个 Data 数据类，约束该记录 data 字段的结构。
信封（SessionRecord）与 Session 类见 session.py；本模块只定义格式，不含读写逻辑。

标签命名沿用两层词汇隔离（参照 DeepSeek-Harness）：
- 带斜杠：会话事件（turn/start、tool/call 等），事实发生时写入
- 不带斜杠：打包存储行（content-chunks 等），多个流式片段攒成一条记录
"""

from dataclasses import dataclass, field, asdict
from typing import Literal
from myagent.agent.core.provider import ChatParams, Message, Usage
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
    turn: int  # 从 1 开始


@dataclass
class StepStartData(SessionData):
    """step/start：一个步骤开始。一步 = 一次 LLM 调用 + 执行其请求的工具。"""
    turn: int
    step: int  # 从 0 开始，循环开始时计数；每个 turn 内重新从 0 计


@dataclass
class RequestHeaderData(SessionData):
    """request/header：一次 LLM 请求的配置快照，仅首次和配置变更时写入。"""
    turn: int | None  # 首次快照写在 turn 1 之前，此时为 None
    step: int | None
    reason: Literal["initial", "resume", "change"]
    model_name: str
    system_prompt: str
    tools: list[dict]  # 工具 schema 列表（Tool.to_schema() 的输出）
    params: ChatParams | None  # 采样参数，None 表示全部用供应商默认值


@dataclass
class UserMessageData(SessionData):
    """user/message：一条进入对话的用户消息。"""
    turn: int
    step: int
    message: Message


@dataclass
class ContentChunksData(SessionData):
    """content-chunks：正文流式片段的打包存储行。"""
    turn: int
    step: int
    dt: list[int]  # 相邻片段的毫秒间隔，长度 = len(texts) - 1
    texts: list[str]  # 逐片段保存不拼接，token 边界是数据


@dataclass
class ReasoningChunksData(SessionData):
    """reasoning-chunks：推理过程流式片段的打包存储行。字段含义同 ContentChunksData。"""
    turn: int
    step: int
    dt: list[int]
    texts: list[str]


@dataclass
class ToolCallChunksData(SessionData):
    """tool-call-chunks：一次工具调用的参数流碎片打包行。"""
    turn: int
    step: int
    index: int  # 槽位号，并行调用时区分归属
    id: str  # 调用标识，整个调用期间恒定，只存一次
    name: str  # 工具名，同上
    dt: list[int]  # 长度 = len(args) - 1
    args: list[str]  # 参数 JSON 碎片逐片保存，单个碎片不是合法 JSON


@dataclass
class AssistantMessageData(SessionData):
    """assistant/message：一次 LLM 调用的完整结果。"""
    turn: int
    step: int
    message: Message
    usage: Usage = field(default_factory=Usage)


@dataclass
class ToolCallData(SessionData):
    """tool/call：一次工具调用，执行时写入、先于 tool/result。"""
    turn: int
    step: int
    call_id: str  # 与 tool/result 配对；并行调用同一工具时靠它区分
    tool_name: str
    arguments: dict  # 已解析的参数


@dataclass
class ToolResultData(SessionData):
    """tool/result：一次工具执行的结果。"""
    turn: int
    step: int
    call_id: str  # 与 tool/call 配对
    tool_name: str
    message: Message  # role="tool"、tool_call_id=call_id 的结果消息
    duration_ms: int | None = None


@dataclass
class StepEndData(SessionData):
    """step/end：一个步骤结束。"""
    turn: int
    step: int


@dataclass
class TurnEndData(SessionData):
    """turn/end：一轮结束。"""
    turn: int
    reason: Literal["success", "interrupted"]


@dataclass
class CompactionStartData(SessionData):
    """compaction/start：压缩事务开始。"""
    compaction_id: str
    turn: int | None  # 轮内压缩填该轮编号；轮与轮之间的独立压缩为 None


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
    turn: int | None





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
            "name" : self.name,
            "created_at":self.created_at.isoformat(),
            "updated_at":updated_at.isoformat(),
            "parent_session_id" : self.parent_session_id,
        }
@dataclass
class SessionRecordData:
    type : str # event的类型
    seq : int # session jsonl的顺序，从非metadata的数据开始
    data : SessionData # RECORD_DATA_TYPES 中的类型实例，落盘时由持久化组件经 to_record_dict 转为 dict
    uuid : str = field(default_factory=lambda : str(uuid.uuid4()))
    timestamp : datetime.datetime = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    surface_op : str | dict = None 
    source_event_seqs : list | None =None # 当surface_op 是{op : replace ,start,end}时必须要 
    

