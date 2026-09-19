---
version: 1.0
created_at: 2026-09-18
updated_at: 2026-09-18
last_updated: 创建本索引，收录 waterfall 订阅契约
abstract: coding-rules 目录索引；收录编写代码时必须遵守的规范、约束与触发场景。
---

# coding-rules 索引

本目录收录**编码规则**类文档：动手写代码时必须遵守的规范与约束。与 `docs/docs-rules/`
（写文档的规则）、`docs/adr/`（为什么做某个决策）分工不同——这里只回答"代码该怎么写"。

按 AGENTS.md 的按需加载规则，**任务涉及代码修改时先读本索引**，再按触发规则加载具体文档。

## 文档格式

每篇 coding-rule 文档必须包含 frontmatter 五字段（`version` / `created_at` / `updated_at`
/ `last_updated` / `abstract`），正文以 `# 标题` 开头。**本目录文档不加 `## 版本` 章节**
（见 `docs/docs-rules/docs-write-rules.md`）。

## 规则清单

### waterfall 订阅契约

- updated_at: 2026-09-18
- path: `docs/coding-rules/2026-09-18-waterfall订阅契约.md`
- 触发规则：注册或修改 `request/error` 等 waterfall 语义事件的订阅方时阅读；改动
  `EventService.emit` / `waterfall` 时阅读
- 内容摘要：waterfall 订阅方必须是 async 可调用对象、委托下游必须 `await _next()`；
  违反时分发器记 ERROR 后返回 `None` 的后果链；emit 与 waterfall 两个派发入口各自
  直接调用（同步 `emit` / 异步 `waterfall`，不设按 spec 分派的包装）
