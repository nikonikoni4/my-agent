# history-bugs 导航

## 还原工具调用 arguments 形态丢失
- updated_at : 2026-10-02
- path: `docs/history-bugs/2026-10-02-还原工具调用arguments形态丢失.md`
- 触发规则：修改 `SessionStore._restore_message` / `SessionStore.load` 的消息还原逻辑、调整 `RawToolCall.arguments` 的两形态约定、或改动 session 记录经 `dataclasses.asdict` 落盘的路径时阅读
- 内容摘要：写侧 `SessionData.to_record_dict()` 走 `asdict`、按内存形态原样落盘（解析成功存 dict），读侧 `_restore_message` 却把非 str 的 `arguments` 一律 `json.dumps` 归一化，还原后 dict 变字符串且中文被 `ensure_ascii=True` 转义成 `\uXXXX`（与 `RawToolCall.raw_arguments` 明确要求的 `ensure_ascii=False` 相左），“还原 == 内存”不变量断裂；附带 `truncated` 未还原。运行时无报错（下游两形态都能吃），修复方向是读侧去掉归一化、原样透传

## 权限拦截短路绕过熔断计数
- updated_at : 2026-09-21
- path: `docs/history-bugs/2026-09-21-权限拦截短路绕过熔断计数.md`
- 触发规则：修改 ToolRegister.execute / _on_failure 熔断计数逻辑、为 execute 执行链新增前置拦截（护栏/限流/审批）短路分支、或为 ToolErrorType 新增错误分类时阅读
- 内容摘要：权限护栏拒绝（Permission_Denied）在 execute 入口短路返回、绕过 _execute 尾部的熔断计数，导致 max_consecutive_failures/raise_on_break 配置失效；修复为拦截分支内补调 _on_failure（取工具须 .get()——护栏工具名表与注册表是两份，下标会 KeyError），附并发批多次抛错一条遗留注意
