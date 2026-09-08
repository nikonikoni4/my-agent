
from dataclasses import dataclass
from typing import Callable

from myagent.infra.events import EventService
import gc
import pytest


@pytest.fixture
def event_service():
    return EventService()


def test_event_service_on(event_service:EventService):
    """
    测试场景：on正常调用是否能正常工作
    
    """
    def callback_test_1():
        print(1)
    def callback_test_2():
        print(2)
    test_event_name = "test"
    event_service.register(test_event_name,callback_test_1)
    # 测试注册是否成功
    assert test_event_name in event_service._hooks , "注册未成功"
    # 测试callback是否正确（存的是弱引用，解引用后应拿回原callback）
    entry, _name = event_service._hooks[test_event_name][0]
    assert entry() is callback_test_1 , "callback不同"
    # 测试第二次添加是否成功
    event_service.register(test_event_name,callback_test_2)
    entry, _name = event_service._hooks[test_event_name][1]
    assert entry() is callback_test_2 , "callback不同"

def test_register_with_non_callable_raises_type_error(event_service:EventService):
    """
    测试场景：错误传入参数类型是否能够正常的报错
    """
    with pytest.raises(TypeError):
        event_service.register("test_event", 123)  # 123 不是可调用对象

def test_on_different_events_are_isolated(event_service:EventService):
    """测试场景：不同的事件是否隔离"""
    event_a = "a"
    event_b = "b"
    def callback_a():
        print("a")
        return "a"
    def callback_b():
        print("b")
        return "b"
    event_service.register(event_a,callback_a)
    event_service.register(event_a,callback_b)
    entry_a, _name = event_service._hooks[event_a][0]
    entry_b, _name = event_service._hooks[event_a][1]
    assert entry_a() is callback_a , "导入不正确"
    assert entry_b() is callback_b , "导入不正确"


def test_emit(event_service:EventService):
    """
    测试场景：测试emit是否能够正常的顺序工作
    """
    @dataclass
    class TestPayLoad:
        c1 = 1
        c2 = 2
    test_event_name = "test"
    exec_log = []
    def callback_test_1(test_payload:TestPayLoad,_next:Callable|None = None):
        exec_log.append(test_payload.c1)
        print(test_payload.c1)
    def callback_test_2(test_payload:TestPayLoad,_next:Callable|None = None):
        exec_log.append(test_payload.c2)
        print(test_payload.c2)
    
    event_service.register(test_event_name,callback_test_1)
    event_service.register(test_event_name,callback_test_2)
    event_service.emit(test_event_name,TestPayLoad())
    assert len(exec_log) ==2 , "emit中的callback未被执行"
    assert exec_log[0] == 1 , "emit中的callback输入参数不对"
    assert exec_log[1] == 2 , "emit中的callback输入参数不对"
    
def test_waterfall_without_finalcallback(event_service:EventService):
    """
    测试场景：测试waterfall无final_callback时是否能正常顺序工作
    """
    @dataclass
    class TestPayLoad:
        c1 = 1
        c2 = 2
    test_event_name = "test"
    exec_log = []
    def callback_test_1(test_payload:TestPayLoad,_next:Callable|None = None):
        assert _next is not None , "waterfall传入的_next为空"
        exec_log.append("c1:进入c1")
        exec_log.append("c1:调用_next进入c2")
        result = _next()
        exec_log.append("c1:从_next中退出")
        assert result == 3, "c1_next接受的结果不对"
    def callback_test_2(test_payload:TestPayLoad,_next:Callable|None = None):
        assert _next is not None , "waterfall传入的_next为空"
        exec_log.append("c2:进入c2")
        exec_log.append("c2:调用_next")
        result = _next()
        exec_log.append("c2:从_next中退出")
        return 3
    
    event_service.register(test_event_name,callback_test_1)
    event_service.register(test_event_name,callback_test_2)
    event_service.waterfall(test_event_name,TestPayLoad())
    assert len(exec_log) ==6 , "emit中的callback未被执行"
    assert exec_log[0] == "c1:进入c1" , "emit中的callback输入参数不对"
    assert exec_log[1] == "c1:调用_next进入c2" , "emit中的callback输入参数不对"
    assert exec_log[2] == "c2:进入c2" , "emit中的callback输入参数不对"
    assert exec_log[3] == "c2:调用_next" , "emit中的callback输入参数不对"
    assert exec_log[4] == "c2:从_next中退出" , "emit中的callback输入参数不对"
    assert exec_log[5] == "c1:从_next中退出" , "emit中的callback输入参数不对"

def test_waterfall_with_finalcallback(event_service:EventService):
    """
    测试场景：测试waterfall有final_callback时是否能正常顺序工作
    """
    @dataclass
    class TestPayLoad:
        c1 = 1
        c2 = 2
    test_event_name = "test"
    exec_log = []
    def callback_test_1(test_payload:TestPayLoad,_next:Callable|None = None):
        assert _next is not None , "waterfall传入的_next为空"
        exec_log.append("c1:进入c1")
        exec_log.append("c1:调用_next进入c2")
        result = _next()
        exec_log.append("c1:从_next中退出")
        assert result == 3, "c1_next接受的结果不对"
    def callback_test_2(test_payload:TestPayLoad,_next:Callable|None = None):
        assert _next is not None , "waterfall传入的_next为空"
        exec_log.append("c2:进入c2")
        exec_log.append("c2:调用_next")
        result = _next()
        assert result ==4 , "final_callback未执行"
        exec_log.append("c2:从_next中退出")
        return 3
    def final_callback(test_payload:TestPayLoad,_next:Callable|None = None):
        exec_log.append("final_callback")
        assert _next() is None , "final_callback调用_next的返回不为空"
        return 4
    event_service.register(test_event_name,callback_test_1)
    event_service.register(test_event_name,callback_test_2)
    event_service.waterfall(test_event_name,TestPayLoad(),final_callback)
    assert len(exec_log) ==7 , "waterfall中的callback未被执行"
    assert exec_log[0] == "c1:进入c1" , "waterfall执行顺序不对"
    assert exec_log[1] == "c1:调用_next进入c2" , "waterfall执行顺序不对"
    assert exec_log[2] == "c2:进入c2" , "waterfall执行顺序不对"
    assert exec_log[3] == "c2:调用_next" , "waterfall执行顺序不对"
    assert exec_log[4] == "final_callback" , "waterfall执行顺序不对"
    assert exec_log[5] == "c2:从_next中退出" , "waterfall执行顺序不对"
    assert exec_log[6] == "c1:从_next中退出" , "waterfall执行顺序不对"


def test_emit_unregistered_event_does_not_raise(event_service: EventService):
    """
    测试场景：emit 调用未注册的事件时不抛异常，直接返回·
    """
    @dataclass
    class TestPayLoad:
        c1 = 1
        c2 = 2
    
    event_service.emit("test",TestPayLoad())
    


def test_emit_callback_exception_does_not_affect_others(event_service: EventService):
    """
    测试场景：emit 中某个回调抛异常时，不影响后续回调的执行
    """
    @dataclass
    class TestPayLoad:
        c1 = 1
        c2 = 2
    test_event_name = "test"
    exec_log = []
    def callback_test_1(test_payload:TestPayLoad,_next:Callable|None = None):
        exec_log.append(test_payload.c1)
        print(test_payload.c1)
    def callback_test_2(test_payload:TestPayLoad,_next:Callable|None = None):
        raise ValueError("测试抛出错误")
    
    event_service.register(test_event_name,callback_test_1)
    event_service.register(test_event_name,callback_test_2)
    event_service.emit(test_event_name,TestPayLoad())
    assert len(exec_log) ==1 , "emit中的callback未被执行"
    assert exec_log[0] == 1 , "emit中的callback输入参数不对"

def test_waterfall_short_circuit(event_service: EventService):
    """
    测试场景：waterfall 中某个回调不调用 _next() 时，后续回调不再执行（短路）
    """
    @dataclass
    class TestPayLoad:
        c1 = 1
        c2 = 2
    test_event_name = "test"
    exec_log = []
    def callback_test_1(test_payload:TestPayLoad,_next:Callable|None = None):
        assert _next is not None , "waterfall传入的_next为空"
        exec_log.append("c1:进入c1")
        exec_log.append("c1:调用_next进入c2")
        result = _next()
        exec_log.append("c1:从_next中退出")
        assert result == 3, "c1_next接受的结果不对"
    def callback_test_2(test_payload:TestPayLoad,_next:Callable|None = None):
        assert _next is not None , "waterfall传入的_next为空"
        exec_log.append("c2:进入c2")
        exec_log.append("c2:调用_next")
        # result = _next()
        exec_log.append("c2:从_next中退出")
        return 3
    def final_callback(test_payload:TestPayLoad,_next:Callable|None = None):
        exec_log.append("final_callback")
        assert _next() is None , "final_callback调用_next的返回不为空"
        return 4
    event_service.register(test_event_name,callback_test_1)
    event_service.register(test_event_name,callback_test_2)
    event_service.waterfall(test_event_name,TestPayLoad(),final_callback)
    assert len(exec_log) ==6 , "waterfall中的callback未被执行"
    assert exec_log[0] == "c1:进入c1" , "waterfall执行顺序不对"
    assert exec_log[1] == "c1:调用_next进入c2" , "waterfall执行顺序不对"
    assert exec_log[2] == "c2:进入c2" , "waterfall执行顺序不对"
    assert exec_log[3] == "c2:调用_next" , "waterfall执行顺序不对"
    assert exec_log[4] == "c2:从_next中退出" , "waterfall执行顺序不对"
    assert exec_log[5] == "c1:从_next中退出" , "waterfall执行顺序不对"

def test_dead_subscriber_auto_cleaned(event_service: EventService):
    """
    测试场景：订阅者对象被回收后注册项自动失效，下次派发前被惰性清理，
    不再对已销毁的订阅者派发
    """
    class Subscriber:
        def on_event(self, payload):
            exec_log.append(payload)
    exec_log = []
    s = Subscriber()
    event_service.register("t", s.on_event)
    event_service.emit("t", 1)
    assert exec_log == [1] , "注册的callback未被执行"

    del s
    gc.collect()
    event_service.emit("t", 2)  # 派发前应清理死引用，不再调用
    assert exec_log == [1] , "已销毁的订阅者不应再被派发"
    assert event_service._hooks["t"] == [] , "失效注册项未被清理"