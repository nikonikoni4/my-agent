# 参考DHS的events.ts编写
# 实现解耦的事件触发

from typing import Any, Callable, Protocol
from enum import Enum
from collections import defaultdict
import logging 
from myagent.infra.events.eventspec import EventSpec
from myagent.infra.events.payload import Payload
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)



# class DispatchMode(str, Enum):
#     """消息派发类型，包含emit，waterfall"""
#     emit = "emit"
#     waterfall = "waterfall"



class EventService:
    """
    实现事件监听解耦
    """
    _hooks:defaultdict[str,list[Callable]] # 事件类型 -> hook序列。 比如 pre-tool-use事件，进行工具调用审批
    
    def __init__(self,):
        self._hooks = defaultdict(list[Callable])
    
    def dispatch(self,):
        pass


    def on(self,event_name,callback:Callable):
        if not isinstance(callback,Callable):
            logger.error("输入的参数类型错误，非callable")
            raise TypeError("输入的参数类型错误，非callable")
        self._hooks[event_name].append(callback)
        

    def emit(self,event_name:str,payload:Payload):
        """
        args:
            event_name:事件名称
            payload : 对应具体事件hook的传递参数，一个@dataclass的类
            
        """
        if event_name not in self._hooks:
            logger.warning(f"{event_name} 未注册")
            return 
        try:
            for callback in self._hooks[event_name]:
                callback(payload)
        except Exception as e:
            # 兜底不让emit导致调用者崩溃
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
        try:
            callback_list = list(self._hooks[event_name]) # 浅拷贝
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
            

    def trigger(self,event_spec:EventSpec,payload):
        """
        使用spec进行触发事件
        """
        if event_spec.semantics == "emit":
            self.emit(event_spec.name,payload)
        elif event_spec.semantics == "waterfall":
            self.waterfall(event_spec.name,payload)