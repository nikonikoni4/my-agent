# lifeprismevalue / evalue —— 评测执行（runner）

本模块承载「记录任务」评测的**执行流程**：读用例 → 跑被测评 agent（+ 可选模拟用户 / 裁判）→ 导出证据 → 裁判判定 → 统计 → 落盘。

- 用例定义、目录结构、字段说明 → [lifeprismTestData/README.md](../../../lifeprismTestData/README.md)
- 评测架构与判定机制（设计层） → [../README.md](../README.md)
- 结构与进程模型的图解（为什么必须一条用例一个进程） → [structure.html](./structure.html)

执行内核是独立的包 [`evaluate`](../../../evaluate)——**环境池 + 并行派发 + 失败现场保留**。本模块只回答「这次跑哪些用例、结果怎么落盘」；环境怎么造、任务怎么铺、失败怎么收，全在 `EvalCore` 里。

| 文件 | 职责 |
| --- | --- |
| `caseload.py` / `types.py` | 读 `cases.yaml` → 用例对象（含校验，环境声明 `meta.env` 也在其中） |
| `env.py` | **机制层**：环境 = 按环境配置（`meta.env`，provider 的构造参数）从只读底座拼的一份数据根（造 / 整份回滚 / 销毁 / 开槽前自检） |
| `worker.py` | **执行通道**：一条任务 = 一条子进程（跨进程协议） |
| `case.py` | **领域层**：单条用例的执行实体（a~j）与子进程入口 `execute_case` |
| `evidence.py` | 取证：与环境基线对比捞「本次写入」（表 / 文件 / 文件与表的全量扫描），并负责打基线快照 |
| `runner.py` | **调度装配**：读 `meta.env` → 组任务 → 交给 `EvalCore` → 收结果 → 让 summary 重建报表 |
| `summary.py` | **报表**：从产物现算 `summary.csv`（列声明表 + 复用 case.py 的派生函数），可单独跑 |

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

**提示词分层**：角色提示词属于 agent 本身，放 `defs/agents/simulator.md`、`defs/agents/judge.md`（含输出 JSON 结构）；用例里只放「本次的具体要求」——simulator 的 `goal` / `known_facts`、judge 的 `rubric`。

---

## 二、前置（一次性，不属于每次 run）

三个 agent 需先各自校准：

- `under_test`：就绪；
- `judge`：用人工标注校准，量出一致率；
- `simulator`：校验「忠实扮演、不越权替 agent 决定记录内容」。

校准结果记入 `defs/agents/` 的版本信息，并写入每次 run 的 `run.json`（grader 版本）。

---

## 三、一次 run 的结构

```
EvalRunner.run(cases_path):
  1. CaseLoader 读 cases.yaml（meta + meta.env + cases[]）
  2. 自检 meta.env（路径 / 库 / keep_tables 在底座里都得在），
     再建 runs/<run_id>/：envs/（槽位环境）ipc/（子进程通信）logs/（子进程日志）sessions/（会话落盘根）
     写 run.json（三个版本轴 + 环境配置 + 起止 + max_workers）
  3. 把用例翻译成 WorkerTask 列表（entrypoint = case.py:execute_case，payload = 用例位置 + 基线目录 + 各路径 + 参数）
  4. EvalCore.run(tasks)：
       开 max_workers 个槽位（一份环境 + 一条子进程）
       逐条任务：取槽位 → 复用前 reset_env（删掉按同一份配置重拼）→ 子进程跑一条用例 → 放回槽位
       失败且 keep_env_on_failure：该槽位退役、现场留在 envs/<key>/，并补一条新槽位
  5. 产出按入参同序映射成 CaseResult → 让 summary 从**刚落盘的产物**重建
     runs/<run_id>/summary.csv，并追加 runs/summary.csv
     （不是拿手里那份内存结果拼表——走同一条路，才保证「删掉 summary 还能重建」一直成立）

一条用例（case.py:execute_case，整体跑在子进程里）：
  0. config.use_data_path(env.root)：切数据根，必须早于创建任何 agent
  a. 打基线快照：把环境当下的样子整份存进 case_dir/baseline/（取证时与它对比）
  b. 应用 precondition：把 case.precondition.rules 写入 custom_prompt.md
  c. case.yaml 快照 → case_dir/
  d~f. 跑 under_test：scripted 按 turns 逐个注入；读本轮终态（turn/end）
  g. 各 agent 的 session 复制改名进 case_dir/（session_under_test.jsonl / _judge.jsonl ...）
  h. 导出 evidence.json：与 baseline/ 对比
  i. 跑 judge（judge.mode=model）→ judge.json；none 则跳过
  j. 跑 stats → stats.json
  收尾. 落 result.json：**只装别处推不出来的三样**（会话起止时间 / 异常原文），
       其余列一律由读方现算 —— 回执里重复存一份，就多出第二个事实来源
```

> **a 步的打点位置很关键**：必须在 precondition **之前**。precondition 写进 `custom_prompt.md` 的规则本身也是证据（有用例要判「规则文件终态」），打在它之后就把这一步藏起来了。
>
> **失败现场**：「运行未正常结束」或「判定不通过」时，跑完后的环境会整份留到 `<用例目录>/env/`，与 `baseline/`（跑之前）成对——`diff -r` 一下就是本次改动。core 的 `keep_env_on_failure` 只管「执行通道失败」，这两类在 core 眼里是**成功**（子进程 exit 0），靠它留不下来。
>
> 另外「重置可变状态」仍然不在这里：环境是用例独占的，槽位复用前由 core 的 `reset_env` 整份回滚，不必再维护「哪些文件会变」的清单。

---

## 四、几个必须注意的点

1. **隔离 = 每用例一份环境 + 独立进程**。`config.use_data_path` 改的是模块级全局，同一进程内两份数据根无法共存——所以执行通道必须是**子进程**（`worker.py`）。推论：`max_workers` 同时决定「占几份环境」与「起几条进程」，也就是并行度上限；`WorkerTask` 里只放可跨进程序列的载荷，注入不了 Python 对象。
2. **环境是按配置拼的，不是「底座整份复制」**（`env.py`）：`cases.yaml` 的 `meta.env` 说清「复制哪些路径、库怎么来」；runner 读出它后作为 **provider 的构造参数**（`env_config`）传进去，**不进 core 的 `EnvSpec`**——core 的契约只认"从哪个只读模板取初始状态"，一旦认识"库 / 表"，换个 provider 就没人能解释这些字段，只能静默忽略（见 `evaluate/core/types.py` 的「解耦纪律」）。底座里**不列就不进环境**——底座实测 393 文件 / 122MB，其中 88% 的文件是日记之类与本次无关的历史，绝大部分体积在库里的日志大表（`window_events` 25 万行等）身上。`db.mode=empty` 只建结构 + 回填 `keep_tables`（枚举 / 注册 / 判分要引用的历史基线）。取舍原则见 `lifeprismTestData/README.md` 的 `env` 一节。
   开跑前还有一道自检（`env.check_env_inputs`）：配置里的路径 / 库 / `keep_tables` 在底座里都得在，`copy` 非空且必须覆盖 `prompts/agent_prompts.yaml`；不满足就整个 run 不启动——名字打错、底座换代这类问题不该表现成「某条用例莫名失败」。
3. **重置是整份回滚**（`env.py`）：删掉环境目录、按同一份配置重新拼一遍（与 `create_env` 共用 `_build`，两条路径不许分叉）。不做「哪些会变」的清单——清单永远会漏，漏了还不报错（下一条用例会继承上一条的写入，表现为结果不对但没异常）。
4. **归因靠与环境基线对比**（`evidence.py`），不再用 `created_at` 时间窗，也不再拿 `base/` 当基线。基线 = 跑用例之前那份环境的初始态快照（`case_dir/baseline/`，`case.py` 的 a 步打）。为什么不能拿 `base/` 当基线：环境只是底座的一个子集，底座里没进环境的东西（几百篇日记、二十万行事件日志）会被全量 diff 判成「本次删掉了」，裁判看到的全是一堆噪声。时间窗的问题则在于只能看见「新写的行」，看不见被改掉、被删掉的东西，也看不见没有时间列的表；对比基线三类都看得见。表有 `id` 列就按 id 对齐比出新增 / 改动 / 删除，没有 id 则退化为整行集合差（`note` 里注明）。
5. **"没写在声明的位置"要专门扫**：表类证据只按用例声明的表取，于是**写到别的表去了**在判分时完全看不见；文本文件同理。所以证据里另有两块全量扫描：`other_changed_files`（未被声明的文本文件）与 `other_changed_tables`（未被声明的表，含"只在一侧存在"的新建 / 删表；行数相等时按逐行指纹判"内容被改过"）。真实教训：一条「时间块备注」用例被 agent 写进了锻炼记录表还顺手打了卡，裁判只看到"声明的那张表 0 行"，于是判成"没做"——而真相是"写错地方"，两者处置完全不同。表扫描刻意不给开关：缺了它会误判归因，而成本有上限（`MAX_SCAN_ROWS`）。
6. **session 只能「跑完复制改名」**：`SessionPresist` 的文件名固定为 `<session_id>.jsonl`，运行时 append 到固定路径，**不支持改名**；且后台每 2 秒批量落盘，复制前必须 flush（`persist_session_now`），否则丢最后一批。不改源码。
7. **判定以落库 / 落盘为准**：模型口头说「已记录」但未落库，算失败。
8. **运行终态要单独区分**：`turn/end` 记录（`TurnEndData`）说明本轮是跑完还是中途失败，带 `reason_type` / `reason_text` / `error_type`——`error_type` 是最外层异常类名（如 `AgentUnclaimedError`、`RetryExhaustedError`、`MaxStepsExceededError`），`reason_text` 是异常链文本（逐层 `类型: 消息`，最多 3 层）。loop 对任何未恢复的异常都以 `error` 收口（含步数上限、重试耗尽），取消为 `interrupted`；据此非 success 就**不判、不重试、停止后续轮次**，否则网络抖动之类会被算成「agent 记错了」，污染结论。`TURN_END` 事件本身不带信息，故只能从 session 读（见 `docs/adr/2026-09-15-session错误信息记录策略.md`）。
9. **失败分三类，现场位置别记混**：
   - **用例级失败**（运行未正常结束、agent 工厂抛错、超时）→ 由 `case.py` 收进 `CaseResult.error`，产物目录里跑到哪算哪（证据 / 统计仍落盘），整次 run 继续；跑完后的环境留在 `<用例目录>/env/`；
   - **执行通道失败**（子进程起不来、崩了、没产出结果）→ 由 `EvalCore` 收成 `WorkerOutcome.ok=False`，runner 落一条只剩错误信息的 `CaseResult`，**现场留在 `envs/<key>/`**（`keep_env_on_failure=False` 则照常回收）；这时用例目录里几乎没有产物（子进程没跑起来）；
   - **判定不通过**（judge 判 `pass=false`）→ 运行本身正常，但结论是"没达标"：跑完后的环境同样留在 `<用例目录>/env/`。
   > 前两类与「判定不通过」的环境现场都在 `<用例目录>/env/`，与 `<用例目录>/baseline/`（跑之前）成对。**注意**：「用例级失败」在 core 眼里是成功（`case.py` 自己兜住了异常、子进程 exit 0），所以 core 的 `keep_env_on_failure` 对它无效——那两类现场只能由领域层自己留（`case.py:_keep_env`）。
10. **子进程日志在 `runs/<run_id>/logs/<用例目录名>.log`**。用例怎么跑的、异常链是什么，先看这个文件；失败时 `worker.py` 会把日志尾部摘进错误信息。
11. **`base/` 只读**：谁都不许写底座，一切写入落在环境里。读底座库也走只读连接（`sqlite_read.open_readonly`）：读 WAL 库会顺带建 `-wal` / `-shm`，那也是写。

---

## 五、测试与试跑

- 测试在 `tests/`（`test_env_provider.py` / `test_worker.py` / `test_evidence.py` / `test_case.py` / `test_summary.py` / `test_runner.py`）：用例级测试用假 agent 同进程跑；run 级测试用假 entrypoint（`tests/fake_case.py`）走真子进程，**不需要 LLM**。
  `test_runner` 里有一条**对拍**：跑一次真链路，把 runner 手里的 `CaseResult` 与从产物重建出来的行逐格比对——两份实现（跑的时候判一次、报表里再算一次）任一处漂了就会红。
  假 entrypoint 必须把产物**按真契约落齐**（形状一律用真函数），否则 run 级测试会在「报表某列为什么是空的」上给出假结论。
  `python -m pytest src/lifeprismevalue/evalue/tests -q`
- 试跑真用例（会真调 LLM、真复制底座）：

  ```python
  from lifeprismevalue.evalue.runner import run_cases

  await run_cases(
      "lifeprismTestData/defs/记录任务-01/cases.yaml",
      base_dir="lifeprismTestData/base",
      runs_dir="lifeprismTestData/runs",
      max_workers=2,
  )
  ```
