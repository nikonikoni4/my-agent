# history-bugs 导航

## 权限拦截短路绕过熔断计数
- updated_at : 2026-09-21
- path: `docs/history-bugs/2026-09-21-权限拦截短路绕过熔断计数.md`
- 触发规则：修改 ToolRegister.execute / _on_failure 熔断计数逻辑、为 execute 执行链新增前置拦截（护栏/限流/审批）短路分支、或为 ToolErrorType 新增错误分类时阅读
- 内容摘要：权限护栏拒绝（Permission_Denied）在 execute 入口短路返回、绕过 _execute 尾部的熔断计数，导致 max_consecutive_failures/raise_on_break 配置失效；修复为拦截分支内补调 _on_failure，附并发批多次抛错与未注册工具 KeyError 两个遗留注意
