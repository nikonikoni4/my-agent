---
version: 1.0
created_at: 2026-09-18
updated_at: 2026-09-18
last_updated: 创建文档，确立 waterfall 订阅方必须为 async 的编码规则及违反后果
abstract: waterfall 语义事件的订阅方必须是 async 可调用对象；本文定义该规则的判据、违反后果、正反例，以及 emit/waterfall 两类派发入口的分工。
---

# waterfall 订阅契约

## 1. 规则

`EventService` 的 waterfall 语义事件（当前为 `request/error`，后续如 `pre-tool-use`），
**所有订阅方都必须是 async 可调用对象**，`waterfall()` 的 `final_callback` 参数同样适用。

订阅方在委托链上下一个订阅方时，必须 `await _next()`，不能写成 `_next()`。

## 2. 判据

waterfall 是**裁决**入口：调用方要拿到裁决值才能继续往下走。裁决本身可能要等外部
应答（人在回路——等用户点"允许/拒绝"），所以订阅方内部需要 `await` 挂起。

这要求分发器是异步的；而异步分发器要 `await` 订阅方，订阅方就必须是 async。

反过来即是本条规则的由来：**同步分发器根本无法 await 订阅方**。若把同步订阅方传进去，
它返回的普通值无法被 await；若把 async 订阅方传给同步分发器，得到的只是一个从未被执行的
协程对象——**整条链的裁决会静默丢失，没有任何报错**。

这不是"异步更时髦"的风格偏好，而是"能不能 await"的硬约束。

## 3. 违反的后果

`waterfall()` 在调用每个订阅方后会检查返回值是否为 awaitable：

- **是** → 正常 await 并继续。
- **否** → 记一条 **ERROR** 日志（含违约订阅方的注册名 + 本文档路径），随后抛
  `TypeError`；该异常被 `waterfall()` 的兜底 `except` 捕获，记 warning 后返回 `None`。

`None` 的既有语义是"无人认领"，所以调用方（如 `ReActAgentLoop._handle_error`）会据此抛
`AgentUnclaimedError`。

**为什么要单独记一条 ERROR**：兜底那条 warning 只说"回调函数错误"，而
`AgentUnclaimedError("无错误处理策略的错误")` 听起来像是"没有配策略"。两者都会把
"某个订阅方写成了同步函数"这个真因掩盖掉。ERROR 那条日志是唯一能指出真因的记录。

## 4. 写法

**正例**

```python
async def on_error(self, payload: RequestErrorPayLoad, _next: callable):
    if isinstance(payload.error_type, self.retry_type):
        return {"decision": "retry", "policy": self.retry_policy}
    return await _next()          # 委托下游必须 await
```

**反例**

```python
def on_error(self, payload, _next):        # ← 缺 async，契约违例
    return _next()                          # ← 即使是非违例的 async 函数，这里也少个 await
```

**注册处不受影响**：`register()` 接受的是 callable，async 绑定方法与同步绑定方法的注册写法
完全一致。契约只约束订阅方自身的定义，不约束注册点。

## 5. 相关约束：两个派发入口各自直接调用

`EventService` 按"调用方要不要等结果"分两个入口，各调各的：

| | 语义 | 入口 | 订阅方 | 调用方写法 |
|---|---|---|---|---|
| emit | 纯通知，无返回值 | `emit(name, payload)` | 同步 | `event_service.emit(SPEC.name, payload)` |
| waterfall | 裁决，有返回值 | `waterfall(name, payload)` | **async** | `await event_service.waterfall(SPEC.name, payload)` |

**不设 `trigger(spec, payload)` 这类按 spec 统一分派的包装**：两种语义的同步性不同，
包进同一个签名会把"调用方要不要等待"藏起来——读调用点时看不出该不该写 `await`，
而这恰恰是本契约最要紧的一件事。直接调用后，`emit(...)` 一眼看出不必等，
`await waterfall(...)` 一眼看出必须等。

emit 的调用点数量多且全是纯通知，它们不该被强加 `await`——没有要等的结果，
在不需要等待的地方阻塞只会拖慢主循环。
