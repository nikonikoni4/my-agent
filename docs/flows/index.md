## agent-loop-错误处理
- updated_at : 2026-09-11
- path: `docs/flows/2026-09-11-agent-loop错误处理.md`
- 触发规则：修改 ToolRegister 错误处理/熔断、ReActAgentLoop 的异常捕获与重试逻辑、llm_retry 策略注册表时阅读
- 内容摘要：agent loop 内两块错误处理——工具调用错误（工具层吸收转错误信息回喂 LLM，不同分类不同话术）与 LLM 调用错误（按来源分类 + request/error 决策 + 重试退避）
