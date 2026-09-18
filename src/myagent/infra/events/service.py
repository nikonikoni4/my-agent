# 参考DHS的events.ts编写
# 实现解耦的事件触发
import inspect, logging, weakref, types
from typing import  Callable
from collections import defaultdict
from myagent.infra.events.eventspec import EventSpec
from myagent.infra.events.payload import Payload
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# waterfall 订阅契约文档位置：报错信息里回指，避免只看到"必须是 async"却找不到依据
_WATERFALL_CONTRACT_DOC = "docs/coding-rules/2026-09-18-waterfall订阅契约.md"

# --- 订阅侧（Hooks）当前存在的问题，待解决 ---
# EventService 内部用 Hooks 列表维护订阅者，on() 不限制注册时机，随时可订阅。
# 这带来两个已知问题，目前均未解决：
# 1. 订阅顺序不可控。emit 按注册先后依次调用回调；waterfall 更是把注册
#    顺序直接固化为洋葱链层级（先注册者在最外层）。当某个订阅要求"必须先于/
#    后于另一个订阅执行"时，目前只能依赖各组件的初始化时序来隐式保证——
#    一旦初始化顺序变动（配置调整、懒加载、导入顺序变化），派发行为会随之
#    静默改变，且没有任何报错提示。
# 2. 回调生命周期（已部分解决）：register 现以 WeakMethod 弱引用存储注册项，
#    订阅方组件被销毁后注册项自动失效，派发前惰性清理，不再长期持有组件、
#    也不会对已销毁组件照常派发。仍未解决：组件"活着但不想再收事件"的
#    语义性解绑（off 接口）缺失；注册 lambda/闭包需调用方自持引用，
#    否则注册后弱引用立即失效。
# 待办：需要引入订阅顺序声明/优先级机制，以及回调解绑接口（off），使组件
# 仍存活时也能主动注销自己的回调，不再依赖初始化时序隐式保证正确性。

# --- 派发入口按"调用方要不要等结果"分成同步与异步两条 ---
# emit（纯通知）   → trigger()          同步。调用方不需要任何返回值。
# waterfall（裁决）→ trigger_waterfall() 异步。调用方必须等到裁决才能继续，
#                    且裁决可能要等外部应答（如人在回路），届时订阅方内部 await。
# 两者不可互串：同步入口 await 不了订阅方，异步入口则会让 emit 的调用方白白
# 等待。误用一律显式抛 TypeError，不做静默降级。
# waterfall 的订阅方必须写成 async——契约见 _WATERFALL_CONTRACT_DOC。


class EventService:
    """
    实现事件监听解耦

    注册项以弱引用(WeakMethod)存储：订阅者对象被回收后注册项自动失效，
    忘记反注册也不会内存泄漏；失效项在每次派发前惰性清理。
    注意：注册 lambda/闭包时调用方必须自己持有该函数的引用，
    否则注册后弱引用立即失效

    派发入口：emit 语义走同步的 trigger()，waterfall 语义走异步的
    trigger_waterfall()（订阅方必须是 async，契约见 _WATERFALL_CONTRACT_DOC）。
    """
    # 事件类型 -> (弱引用, 注册时记录的callback名) 序列。 比如 pre-tool-use事件，进行工具调用审批
    _hooks: defaultdict[str, list[tuple[weakref.ReferenceType, str]]]

    def __init__(self,):
        self._hooks = defaultdict(list)
    
    def dispatch(self,):
        pass


    def register(self,event_name,callback:Callable):
        if not isinstance(callback,Callable):
            logger.error("输入的参数类型错误，非callable")
            raise TypeError("输入的参数类型错误，非callable")
        # bound method 用 WeakMethod（弱引用实例本身，普通 ref 会因 bound method
        # 每次访问新建对象而立刻失效）；其他 callable（普通函数/lambda/实现了
        # __call__ 的实例）用普通弱引用，两者统一用 entry() 取回 callback
        # （已死返回 None）。名字必须在注册时记录：弱引用失效后原对象已被
        name = getattr(callback, "__qualname__", None) or repr(callback)
        if isinstance(callback, types.MethodType):
            entry = weakref.WeakMethod(callback)
        else:
            entry = weakref.ref(callback)
        self._hooks[event_name].append((entry, name))
        

    def emit(self,event_name:str,payload:Payload):
        """
        args:
            event_name:事件名称
            payload : 对应具体事件hook的传递参数，一个@dataclass的类
            
        """
        if event_name not in self._hooks:
            logger.debug(f"{event_name} 未注册")
            return
        self._clear_dead_callback(event_name)
        for entry, _name in list(self._hooks[event_name]): # 浅拷贝，防callback在迭代中注册/注销打乱迭代
            callback = entry()  # 解引用：取回callback；None 表示订阅者已被回收
            if callback is None:
                continue
            try:
                callback(payload)
            except Exception as e:
                # 兜底：单个callback出错只跳过它自己，不影响其余callback和调用者
                logger.warning(f"emit 回调函数错误: {e}")
            
    
    async def waterfall(self,event_name:str,payload:Payload,final_callback:Callable|None =None):
        """
        args:
            event_name:事件名称
            payload : 对应具体事件hook的传递参数，一个@dataclass的类
            final_callback ： 洋葱中间件最里层的callback，会在_hooks[event_name]的所有调用完成之后调用，默认为None

        订阅契约：所有订阅方（含 final_callback）都必须是 async 可调用对象。
        同步订阅方在这里无法被 await，混入后整条链的裁决都会丢失，故按编程
        错误处理——记 ERROR 日志（含订阅方名字，避免被下面的兜底 warning
        掩盖成"无人认领"）后抛 TypeError。契约见 _WATERFALL_CONTRACT_DOC。

        Returns:
            洋葱链最外层订阅方的裁决值；无订阅方时返回 None。
        """
        if event_name not in self._hooks:
            logger.debug(f"{event_name} 未注册")
            return None
        self._clear_dead_callback(event_name)
        try:
            # 解引用弱引用并过滤失效项（保持原先浅拷贝语义）；同时保留注册时
            # 记录的名字——契约违例的报错信息要靠它指出是谁
            callback_list:list[tuple[Callable,str]] = []
            for entry, name in self._hooks[event_name]:
                cb = entry()
                if cb is not None:
                    callback_list.append((cb, name))
            if final_callback is not None:
                callback_list.append((final_callback, getattr(final_callback, "__qualname__", None) or repr(final_callback)))
            async def _next():
                if len(callback_list) == 0:
                    return None
                callback, name = callback_list.pop(0)
                # callback 约定最后一个参数是_next回调函数，用于调用下一个callback
                result = callback(payload,_next)
                if not inspect.isawaitable(result):
                    logger.error(
                        f"waterfall 订阅方 {name} 未返回 awaitable（不是 async 函数），"
                        f"违反订阅契约，本次裁决作废；契约见 {_WATERFALL_CONTRACT_DOC}"
                    )
                    raise TypeError(
                        f"waterfall 订阅方 {name} 必须是 async 函数（契约见 {_WATERFALL_CONTRACT_DOC}）"
                    )
                return await result
            return await _next()
        except Exception as e:
            # 兜底不让waterfall导致调用者崩溃
            logger.warning(f"waterfall 回调函数错误: {e}")
            return None

    def _clear_dead_callback(self,event_name):
        """
        清理失效的弱引用（订阅者对象已被GC回收的注册项）
        """
        entries = self._hooks.get(event_name)
        if not entries:
            return
        alive = []
        for entry, name in entries:
            if entry() is None:  # 解引用返回 None = 订阅者已被回收
                logger.info(f"清理失效的callback: {name}")
                continue
            alive.append((entry, name))
        self._hooks[event_name] = alive
    def trigger(self,event_spec:EventSpec,payload):
        """
        使用spec同步触发 emit 语义事件（纯通知，无返回值）。

        waterfall 语义事件必须走 async 的 trigger_waterfall：本方法是同步的，
        既拿不到也无法 await 订阅方，误用会让整条裁决链静默失效，故在此
        显式报错而不是把一个协程对象丢掉。
        """
        if event_spec.semantics != "emit":
            raise TypeError(
                f"{event_spec.name} 是 {event_spec.semantics} 语义，"
                f"必须用 await trigger_waterfall() 触发"
            )
        self.emit(event_spec.name,payload)

    async def trigger_waterfall(self,event_spec:EventSpec,payload):
        """
        使用spec异步触发 waterfall 语义事件，返回洋葱链最外层订阅方的裁决值
        （作为控制信号传回调用方，如错误处理订阅方决定"继续/终止/人工确认"）。

        Returns:
            裁决值；无订阅方或订阅方全部失效时返回 None（调用方据此判定
            "无人认领"）。
        """
        if event_spec.semantics != "waterfall":
            raise TypeError(
                f"{event_spec.name} 是 {event_spec.semantics} 语义，"
                f"应用同步的 trigger() 触发"
            )
        return await self.waterfall(event_spec.name,payload)