---
version: 2.3
created_at: 2026-09-03
updated_at: 2026-09-19
last_updated: 同步"工具调用解析与截断处置移入工具层"条目为 v2.1（payload 改整批 + session 的 arguments 按拿到时的形态落盘）
abstract: agent 项目架构决策索引。
---

| 日期 | 决策标题 | 状态 | 项目名 | 详情 |
|------|---------|------|--------|------|
| 2026-09-15 | agent loop 错误处理重构（2 个 except + finally 三阶段 + 终止统一抛出 + 预算检查并入） | decided | agent | [详情](.\2026-09-15-agent-loop错误处理重构.md) |
| 2026-09-15 | 异常分类树新增策略域与未知域（AgentPolicyError 窄域 + AgentUnknownError 兜底 + 一级域按"终结路径"划分） | decided | agent | [详情](.\2026-09-15-异常分类树新增策略域与未知域.md) |
| 2026-09-15 | session 错误信息记录策略（类别 + 异常链文本 + 限深 3 层；llm/retry 只记真正重试） | decided | agent | [详情](.\2026-09-15-session错误信息记录策略.md) |
| 2026-09-10 | cancel 取消并结束循环（与 asyncio 对齐 + 取消逐层传播到 _loop 消化 + send 按需重启，删除 stop） | decided | agent | [详情](.\2026-09-10-cancel取消并结束循环.md) |
| 2026-09-10 | LLM 重试延迟退避策略（统一指数退避公式 + 参数放策略注册表 + 优先 Retry-After） | decided | agent | [详情](.\2026-09-10-LLM重试延迟退避策略.md) |
| 2026-09-10 | LLM 调用失败时的 session 补齐策略（保留已产出 chunk + 补 error finish 块 + 不补 assistant/message） | decided | agent | [详情](.\2026-09-10-LLM调用失败时的session补齐策略.md) |
| 2026-09-10 | 工具调用解析与截断处置移入工具层 v2.1（provider 尽力解析 str\|dict + 工具层信任边界回喂 + finish 链路承载截断强调 + payload 改整批 + session 的 arguments 按拿到时的形态落盘） | decided | agent | [详情](.\2026-09-10-工具调用解析与截断处置移入工具层.md) |
| 2026-09-09 | 工具调用截断与 JSON 解析错误分类（平铺并列、突出 Max Token 截断的三类成因） | superseded | agent | [详情](.\2026-09-09-工具调用截断与JSON解析错误分类.md) |
| 2026-09-09 | LLM 错误处理分类策略（内部翻译 + 单点捕获 + 按来源分类树 + 独立策略注册表） | decided | agent | [详情](.\2026-09-09-LLM错误处理分类策略.md) |
| 2026-09-07 | 会话文件夹命名与读取分类（有损名 + uuid 后缀 + 扫描 meta） | decided | agent | [详情](.\2026-09-07-会话文件夹命名与读取分类.md) |
| 2026-09-04 | Session 持久化写入两级回滚（with 非原子前提下的整批原子性） | decided | agent | [详情](.\2026-09-04-session持久化写入两级回滚.md) |
| 2026-09-03 | Session 承担过程日志职责（从仅存 Message 到过程日志） | decided | agent | [详情](.\2026-09-03-session承担过程日志职责.md) |
