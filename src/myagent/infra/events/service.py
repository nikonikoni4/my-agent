# 参考DHS的events.ts编写
# 实现解耦的事件触发
import logging, weakref, types
from typing import  Callable
from collections import defaultdict
from myagent.infra.events.eventspec import EventSpec
from myagent.infra.events.payload import Payload
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

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


class EventService:
    """
    实现事件监听解耦

    注册项以弱引用(WeakMethod)存储：订阅者对象被回收后注册项自动失效，
    忘记反注册也不会内存泄漏；失效项在每次派发前惰性清理。
    注意：注册 lambda/闭包时调用方必须自己持有该函数的引用，
    否则注册后弱引用立即失效
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
            logger.warning(f"{event_name} 未注册")
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
            
    
    def waterfall(self,event_name:str,payload:Payload,final_callback:Callable|None =None):
        """
        args:
            event_name:事件名称
            payload : 对应具体事件hook的传递参数，一个@dataclass的类
            final_callback ： 洋葱中间件最里层的callback，会在_hooks[event_name]的所有调用完成之后调用，默认为None
        
        """
        if event_name not in self._hooks:
            logger.warning(f"{event_name} 未注册")
            return 
        self._clear_dead_callback(event_name)
        try:
            # 解引用弱引用并过滤失效项（保持原先浅拷贝语义）
            callback_list = [cb for cb in (entry() for entry, _ in self._hooks[event_name]) if cb is not None]
            if final_callback is not None:
                callback_list.append(final_callback)
            def _next():
                if len(callback_list) == 0:
                    return None
                callback = callback_list.pop(0)
                return callback(payload,_next) # callback 约定最后一个参数是_next回调函数，用于调用下一个callback
            return _next()
        except Exception as e:
            # 兜底不让waterfall导致调用者崩溃
            logger.warning(f"waterfall 回调函数错误: {e}")
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
        使用spec进行触发事件
        """
        if event_spec.semantics == "emit":
            self.emit(event_spec.name,payload)
        elif event_spec.semantics == "waterfall":
            self.waterfall(event_spec.name,payload)