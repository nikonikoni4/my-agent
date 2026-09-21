# 领域术语表

本仓库的领域语言与概念。命名（issue 标题、重构提案、测试名）一律用这里的词，不要
漂移到同义词。约定见 `docs/agents/domain.md`。

---

## turn 终态 / `reason_type`

`turn/end` 记录上的一格：这一轮**怎么收场的**。三选一。

判据是**控制流事实**，不是"过程中有没有出错"——出过错但被处置吸收（退避重试成功、
放宽预算后继续）仍算 `success`。

| 取值 | 含义 | 触发路径 |
| --- | --- | --- |
| `success` | 正常完成 | 一路无事；或错误被处置成"继续"（`retry` / `backoff_retry` / `continue`）后最终收敛 |
| `interrupted` | 主动停止 | 用户取消（`CancelledError`）；或**策略终止**——决策为 `break` 但未声明按失败上报（如人在回路选"取消"） |
| `error` | 意外且未被吸收 | 任何上抛的异常：声明了按失败上报的 `break`（`as_error`）、无人认领、重试耗尽、其它未归一的异常 |

判定位置：`ReActAgentLoop.turn` 与 `_resolve_reason`。上抛类走 `except` 分支落定；
正常返回类读 `agent/error-handle` 账本的**最后一条**（`Session.error_handles`）。

`interrupted` 的两种来源靠 `reason_text` 区分：取消固定写 `用户主动打断`，策略终止写
`策略终止：{触发它的错误类名}`。

相关：`agent/error-handle`、`decision`、`as_error`。
