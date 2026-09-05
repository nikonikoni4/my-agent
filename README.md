这是一个用于学习搭建一个稳定，可观测agent。

# MVP 

1. LLM调用（先只使用MiMo模型）
2. 消息处理与多轮对话。
2. 工具调用循环
3. MCP
4. skill
5. llm调用log机制、工具评估机制（"D:\desktop\quackDocs\my_notes\知识库\raw\personal-notes\agent开发\agent设计.md"仅供参考）、记忆机制（方案未定）

做到：1. 错误记录 2. 错误处理（重试+hit提示等） 3. 功能降级处理 4. 人工干预审核 5. 沙箱机制，权限请求等等

# 可参考
 D:\desktop\软件开发\deepseek-harness 
 D:\desktop\软件开发\nanobot
 

也有这样的场景，而且它回复了很多内容，但其中有很多词句我不理解，所以我需要进行询问。但在理解它本身回答内容的过程中，不应该占用整个主问题的 context，所以需要设置一些 by the way（claude code的一个附加窗口问答）。但是比如说 Claude Code 里面的 by the way 提问方式很不友好（只能提问一次或新的fork也是一个新的窗口，这些问题并不能在视觉或者信息上进行整合，比如关键词是在fork中解释的，但是无法以一种备注的方式展示在原窗口），所以这里需要一个特别的功能（可对话的备注？），专门去对每一个 answer 进行 explain。比如说点击一个 Answer，可以在里面新开会话，在一个小的会话里进行多轮对话。实际上这也是一个 Fork，点击新会话后会 Fork 一个会话，然后这个 Fork 会话会附着在上面。