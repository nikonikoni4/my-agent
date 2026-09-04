"""会话与持久化：定义 Session 数据模型和 SessionManager 存取器。

Session 只负责消息数据和元信息；落盘格式为 jsonl（第一行 meta_data，
之后每行一条 Message 的持久化 dict）。
"""
from dataclasses import dataclass,field
from myagent.agent.core.provider import Message, ToolCallRequest
import datetime
import uuid,json
from pathlib import Path

@dataclass
class SessionRecord:
    """
    session文件每一行保存的单位
    """
    uuid : str = field(default_factory=lambda : str(uuid.uuid4()))
    turn : int  # 每次写入+1
    message : Message 
    

@dataclass
class Session:
    """
    Attributes:
        messages : 消息列表
        last_compact_loc : 上一次压缩位置
        created_at : 创建时间 str  UTC isoformat
        updated_at : 更新时间 str  UTC isoformat
        name : 会话名称
    note : 
        
    """
    session_id : str = field(default_factory=lambda : str(uuid.uuid4()))
    name : str = ""  # 为空时在 __post_init__ 中取 session_id
    last_compact_loc : int = 0
    created_at : str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    updated_at : str = ""
    messages : list[Message] = field(default_factory=list)
    

    def __post_init__(self):
        """name 为空时回退为 session_id，保证每个会话总有可读的名称。"""
        if not self.name:
            self.name = self.session_id

    def add_message(self,message : Message):
        """向会话追加一条消息。

        Args:
            message: 待追加的 Message 实例。

        Raises:
            TypeError: message 不是 Message 类型时抛出。
        """
        if not isinstance(message,Message):
            raise TypeError(f"输入的类型错误 ")
        self.messages.append(message)

    def meta_data(self,updated_at:str |None = None)->dict:
        """生成会话的元信息 dict（落盘文件第一行的内容）。

        Args:
            updated_at: 显式指定的更新时间；不传时优先用实例的 updated_at，
                实例也为空则取当前 UTC 时间。

        Returns:
            含 type/session_id/name/last_compact_loc/created_at/updated_at 的 dict。
        """
        if not updated_at:
            updated_at =self.updated_at if self.updated_at else datetime.datetime.now(datetime.timezone.utc).isoformat() 
        return {
            "type":"meta_data",
            "session_id":self.session_id,
            "name" : self.name,
            "last_compact_loc":self.last_compact_loc,
            "created_at":self.created_at,
            "updated_at":updated_at
        }

    def compact(self):
        """压缩历史消息（长对话摘要等），尚未实现。"""
        pass

class SessionManager:
    """会话管理器：维护 id 到 Session 的内存缓存，并负责落盘与从文件还原。"""

    def __init__(self,session_file_path : Path | str | None):
        """初始化会话管理器。

        Args:
            session_file_path: 会话 jsonl 文件所在目录；传 str 会转为 Path，
                传 None 时使用默认目录 ./localData/session。
        """
        self._sessions : dict[str,Session]= {} # id ->
        if session_file_path and isinstance(session_file_path,str):
            self._session_file = Path(session_file_path)
        elif isinstance(session_file_path,Path):
            self._session_file = session_file_path
        else:
            self._session_file = Path("./localData/session")
    def load_or_create_session(self,session_id :str | None = None)->Session:
        """按 id 取会话：内存缓存 → 文件加载 → 都没有则新建"""
        # 1. 命中内存缓存直接返回
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        if session_id:
            # 2. 从文件加载（文件名约定：{session_id}.jsonl）
            session = self._load_session_from_file(self._session_file / f"{session_id}.jsonl")
            # 3. 文件不存在，按给定 id 新建
            if session is None:
                session = Session(session_id=session_id)
        else:
            # 未指定 id：全新会话（id 和 name 由 Session 自动生成）
            session = Session()
        self._sessions[session.session_id] = session
        return session

    def save_session(self,session_id :str):
        """把指定会话落盘为 jsonl 文件（整体覆写）

        已知限制：每次保存都全量重写该会话文件；消息量大时可改为只追加新消息，
        目前按简单方案处理。
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"会话 {session_id} 不在内存中，无法保存")
        self._session_file.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        session.updated_at = now
        lines = [json.dumps(session.meta_data(now), ensure_ascii=False)]
        lines.extend(json.dumps(m.to_dict_with_timestamp(), ensure_ascii=False) for m in session.messages)
        file_path = self._session_file / f"{session.session_id}.jsonl"
        file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _load_session_from_file(self,file_path : Path)->Session|None:
        """从 jsonl 文件还原会话；文件不存在返回 None

        文件格式约定：第一行为 meta_data 行，其余每行为一条 Message 的 wire 格式 dict
        """
        if not file_path.exists():
            return None
        session = Session()
        with file_path.open('r',encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:  # 跳过空行
                    continue
                data :dict = json.loads(line)
                if data.get("type") == "meta_data":
                    # session_id 先恢复，name 为空时才能正确回退到恢复后的 id
                    session.session_id = data.get("session_id") or session.session_id
                    session.name = data.get("name") or session.session_id
                    session.last_compact_loc = data.get("last_compact_loc", 0)
                    session.created_at = data.get("created_at") or session.created_at
                    session.updated_at = data.get("updated_at") or session.created_at
                else:
                    session.messages.append(self._message_from_dict(data))
        return session

    @staticmethod
    def _message_from_dict(data : dict)->Message:
        """把 wire 格式的 dict 还原为 Message（to_dict 的逆操作）"""
        tool_calls = None
        if data.get("tool_calls"):
            tool_calls = [
                ToolCallRequest(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=json.loads(tc["function"]["arguments"]),
                )
                for tc in data["tool_calls"]
            ]
        return Message(
            role=data["role"],
            content=data.get("content"),
            tool_calls=tool_calls,
            tool_call_id=data.get("tool_call_id"),
            reasoning_content=data.get("reasoning_content"),
            timestamp=data.get("timestamp")
        )
                    
    def get_session_id_from_files(self)->list[str]:
        """列出目录下已落盘的所有会话 id。

        Returns:
            各 jsonl 文件名去掉后缀组成的列表，即 session_id 列表。
        """
        # 打开文件夹，获取文件夹下所有的jsonl的文件名称
        # 文件名（不含 .jsonl 后缀）即 session_id
        return [p.stem for p in self._session_file.glob("*.jsonl")]

    