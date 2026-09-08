from typing import Literal
from myagent.infra.events.payload import (
    Payload,
    TurnStartPayload,
    StepStartPayload,
    RequestHeaderPayload,
    UserMessagePayload,
    AssistantChunkPayload,
    AssistantMessagePayload,
    ToolCallPayload,
    ToolResultPayload,
    StepEndPayload,
    TurnEndPayload,
    SessionEventPayload,
    RequestErrorPayLoad
)

class EventSpec:
    """
    事件类型约定
    """
    def __init__(self,name:str,semantics:Literal["waterfall","emit"],payload_type:type[Payload]):
        self.name = name
        self.semantics = semantics
        self.payload_type = payload_type

# evnet事件集中定义，内容过多时分模块文件导出
# 事件名称遵循 域/动作 格式
# 注意：本文件只登记运行时事件。chunk 的打包存储行（content-chunks 等）是
# session 落盘时的存储编码，不是运行时事件，登记在 core/session/types.py
# 触发语义目前统一为 emit，waterfall 类型后续按需添加

# --- 订阅侧（Hooks）当前存在的问题，待解决 ---
# EventService 内部用 Hooks 列表维护订阅者，on() 不限制注册时机，随时可订阅。
# 这带来两个已知问题，目前均未解决：
# 1. 订阅顺序不可控。emit 按注册先后依次调用回调；waterfall 更是把注册
#    顺序直接固化为洋葱链层级（先注册者在最外层）。当某个订阅要求"必须先于/
#    后于另一个订阅执行"时，目前只能依赖各组件的初始化时序来隐式保证——
#    一旦初始化顺序变动（配置调整、懒加载、导入顺序变化），派发行为会随之
#    静默改变，且没有任何报错提示。
# 2. 回调生命周期无人回收。注册只是把回调引用追加进列表，没有对应的解绑
#    接口。订阅方组件被销毁后，其回调仍挂在 Hooks 中，事件触发时会照常
#    调用——轻则回调引用长期持有导致组件无法释放，重则回调内部访问已
#    关闭/已失效的资源，产生脏状态或运行时异常（而 emit/waterfall 会
#    兜底吞掉这类异常，仅记日志，问题更难被发现）。
# 待办：需要引入订阅顺序声明/优先级机制，以及回调解绑接口（off），使组件
# 销毁时能够注销自己的回调，不再依赖初始化时序隐式保证正确性。

TURN_START = EventSpec("turn/start", "emit", TurnStartPayload)
STEP_START = EventSpec("step/start", "emit", StepStartPayload)
REQUEST_HEADER = EventSpec("request/header", "emit", RequestHeaderPayload)
USER_MESSAGE = EventSpec("user/message", "emit", UserMessagePayload)
ASSISTANT_CHUNK = EventSpec("assistant/chunk", "emit", AssistantChunkPayload)
ASSISTANT_MESSAGE = EventSpec("assistant/message", "emit", AssistantMessagePayload)
TOOL_CALL = EventSpec("tool/call", "emit", ToolCallPayload)
TOOL_RESULT = EventSpec("tool/result", "emit", ToolResultPayload)
STEP_END = EventSpec("step/end", "emit", StepEndPayload)
TURN_END = EventSpec("turn/end", "emit", TurnEndPayload)
SESSION_EVENT = EventSpec("session/event", "emit", SessionEventPayload)
REQUEST_ERROR = EventSpec("request/error","waterfall",RequestErrorPayLoad)
# 集中聚合所有事件，用于完整性校验（名称唯一、命名规范）
ALL_EVENTS: list[EventSpec] = [
    TURN_START,
    STEP_START,
    REQUEST_HEADER,
    USER_MESSAGE,
    ASSISTANT_CHUNK,
    ASSISTANT_MESSAGE,
    TOOL_CALL,
    TOOL_RESULT,
    STEP_END,
    TURN_END,
    SESSION_EVENT
]
