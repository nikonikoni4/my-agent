# lifeprismevalue / evalue —— 评测执行（runner）

本模块承载"记录任务"评测的**执行流程**：读用例 → 跑被测评 agent（+ 可选模拟用户 / 裁判）→ 导出证据 → 裁判判定 → 统计 → 落盘。

- 用例定义、目录结构、字段说明 → [lifeprismTestData/README.md](../../../lifeprismTestData/README.md)
- 评测架构与判定机制（设计层） → [../README.md](../README.md)

---

## 一、角色与开关

| 角色 | 说明 | 何时存在 |
| --- | --- | --- |
| `under_test` | 被测评 agent | 总是 |
| `simulator` | 模拟用户（输入方），动态接话 | 仅 `input_mode: agent` |
| `judge` | 裁判（评估方） | 仅 `judge.mode: model` |

两个开关**相互独立**（不是绑定）：

- `input_mode`：`scripted`（按 `turns` 条件注入）/ `agent`（由 simulator 驱动）。
- `judge.mode`：`model`（裁判 agent）/ `none`（不判，仅留证据）。

**提示词分层**：角色提示词属于 agent 本身，放 `defs/agents/simulator.md`、`defs/agents/judge.md`（含输出 JSON 结构）；用例里只放"本次的具体要求"——simulator 的 `goal` / `known_facts`、judge 的 `rubric`。

---

## 二、前置（一次性，不属于每次 run）

三个 agent 需先各自校准：

- `under_test`：就绪；
- `judge`：用人工标注校准，量出一致率；
- `simulator`：校验"忠实扮演、不越权替 agent 决定记录内容"。

校准结果记入 `defs/agents/` 的版本信息，并写入每次 run 的 `run.json`（grader 版本）。

---

## 三、每次 run 的流程

```
run(cases_path):
  1. CaseLoader 读 cases.yaml（meta + cases[]）
  2. 准备 base/（只读）；建 runs/<run_id>/；写 run.json（dataset/prompt/model/grader 四版本 + 起止）

  3. for i, case in enumerate(cases):
       cdir = runs/<run_id>/<meta.id>/<i:03d>_<case.id>/

       a. 重置可变状态：DB 还原 pristine；custom_prompt.md 还原基线
       b. 应用 precondition：把 case.precondition.rules 写入 custom_prompt.md
       c. case.yaml 快照 → cdir/case.yaml
       d. 记 t_start（UTC）

       e. 运行 under_test：
          - input_mode=scripted → 按 turns 逐个注入（带 trigger 的等条件命中）
          - input_mode=agent    → simulator 按 goal/known_facts 驱动；
                                  handover_after 前 N 轮走脚本，之后交给 simulator
          - 三个 agent 各建自己的 Session（name 分别 under_test / simulator / judge）

       f. 记 t_end（UTC）

       g. 落盘 + 复制改名：
          - 先 flush（调 presist() / 等 ≥2s / 停持久化循环），否则丢最后一批
          - 按各自 session_id 找到 <id>.jsonl，复制并改名为：
              session_under_test.jsonl / session_simulator.jsonl / session_judge.jsonl → cdir/

       h. 导出 evidence.json：
          - DB 表：WHERE created_at ∈ [t_start, t_end]
          - 文件类（.md）：运行前/后快照 diff 或取终态（日记路径解析成当天日期）

       i. 跑 judge（judge.mode=model；none 则跳过）：
          - 输入 = defs/agents/judge.md 的角色提示词 + case.rubric
                   + evidence.json + under_test session 的 user/assistant/tool_result 文本
          - 输出 → cdir/judge.json（pass / reason / 可选：轨迹、分项）

       j. 跑 stats（通用组件，对 under_test session）→ cdir/stats.json（token / 耗时 / 路径）
       k. 追加一行 → runs/<run_id>/summary.csv（同时追加全局 runs/summary.csv）
```

> `stats`（j）不依赖裁判，可与 `judge`（i）并行；也可先算 stats、把"路径 / token 摘要"一并喂给 judge 作辅助。

---

## 四、几个必须注意的点

1. **重置可变状态（步骤 a）**：用例共用 `base/`，跑新用例前必须把上一个用例改过的 `custom_prompt.md`（及 DB）还原，否则规则跨用例污染、且 agent 读取"最新记录"会让结果不可复现。
2. **session 只能"跑完复制改名"（步骤 g）**：`SessionPresist` 的文件名固定为 `<session_id>.jsonl`，运行时 append 到固定路径，**不支持改名**；且后台每 2 秒批量落盘，复制前必须 flush，否则丢最后一批。不改源码。
3. **evidence 分两类取法（步骤 h）**：DB 表按 `created_at` 时间窗筛（用写入时间而非业务时间 `event_time`，因为可能"记录昨天的事"）；`.md` 文件没有时间戳，只能前后快照 diff 或取终态，日记路径需在运行时解析成当天日期。
4. **判定以落库 / 落盘为准**：模型口头说"已记录"但未落库，算失败。
