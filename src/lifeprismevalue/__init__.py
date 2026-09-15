"""lifeprismevalue 模块

复用 lifeprism 的 Agent 数据工具与数据，结合 myagent 框架对原 lifeprism 的
agent 提示词与工具进行评估。

包含：
- 移植自 LifeWatch-AI 的 16 个数据类 Agent 工具（行为 / 心情 / 习惯 / 自定义记录 / 会话）
- 统一的 sqlite 数据访问层与本地时区处理
- 以 myagent.agent.core.tool.Tool 为基类，可直接注册进 myagent 的工具注册表
"""

__version__ = "0.1.0"