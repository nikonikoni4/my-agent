"""评测用例的数据结构（cases.yaml 的内存形态）。

字段含义见 lifeprismTestData/README.md 的「用例字段」一节。
本模块只定义结构，解析与校验在 caseload.py。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# input_mode：谁产生用户轮
INPUT_MODE_SCRIPTED = "scripted"  # 按 turns 条件注入
INPUT_MODE_AGENT = "agent"        # 由 simulator 驱动
INPUT_MODES = (INPUT_MODE_SCRIPTED, INPUT_MODE_AGENT)

# db.mode：库的两种来法。**lifeprism 数据根的存储知识**，属于本包（领域侧）：
# 它说的是"lifewatch_ai.db 这个 sqlite 库怎么造"，与环境机制无关的通用词汇里没有它。
# 所以它不进 `evaluate.core` 的 `EnvSpec`，只在本包内流通（解析 + 机制实现都从这里取）。
DB_MODE_COPY = "copy"      # 整库复制：结构 + 全部行
DB_MODE_EMPTY = "empty"    # 只建结构：行只保留 keep_tables 里列的
DB_MODES = (DB_MODE_COPY, DB_MODE_EMPTY)

# judge.mode：谁判
JUDGE_MODE_MODEL = "model"        # 裁判 agent
JUDGE_MODE_NONE = "none"          # 不判，仅留证据
JUDGE_MODES = (JUDGE_MODE_MODEL, JUDGE_MODE_NONE)


@dataclass
class Turn:
    """一轮用户输入。

    trigger 仅多轮用：agent 上一轮在问什么，才注入这一轮（条件注入）。
    """

    text: str
    role: str = "user"
    trigger: str | None = None


@dataclass
class Precondition:
    """运行前对环境的设置。"""

    rules: list[str] = field(default_factory=list)  # 写入 custom_prompt.md 的关联记录规则
    fixture: dict = field(default_factory=dict)     # 预置数据（占位，暂未使用）


@dataclass
class Simulator:
    """input_mode=agent 时，模拟用户 agent 的「本次要求」（角色提示词在 agent 定义里）。"""

    goal: str = ""
    known_facts: list[str] = field(default_factory=list)  # 模拟 agent 只能依据这些作答
    handover_after: int = 0  # 前 N 轮走 turns，之后交给 simulator；0 = 全程 agent


@dataclass
class Judge:
    """裁判配置（裁判提示词在 agent 定义里）。"""

    mode: str = JUDGE_MODE_MODEL


@dataclass
class DbConfig:
    """环境里数据库的来法（cases.yaml 的 `meta.env.db`）。

    这是 lifeprism 的存储知识：库在哪、整库复制还是只建空壳、空壳里留哪几张表。
    它**不进** `evaluate.core` 的 `EnvSpec`（那儿的词汇必须与机制无关），
    而是作为环境机制的构造参数传给 provider（见 [env.py](./env.py)）。
    """

    path: str = ""                              # 相对底座的路径，如 dataset/lifewatch_ai.db
    mode: str = DB_MODE_EMPTY                   # copy（整库复制）| empty（只建结构）
    keep_tables: list[str] = field(default_factory=list)  # mode=empty 时整表保留的表名

    def __post_init__(self) -> None:
        """模型层的不变量（解析层另有带字段路径的报错，这里是程序构造时的兜底）。"""
        if self.mode not in DB_MODES:
            raise ValueError(f"DbConfig.mode 非法: {self.mode!r}（应为 {DB_MODES}）")
        if self.keep_tables and self.mode != DB_MODE_EMPTY:
            # 写了却被忽略的声明是最坏的一种：看着像生效了，其实没有
            raise ValueError(f"mode={self.mode} 时 keep_tables 无意义（整库都复制了）")


@dataclass
class EnvConfig:
    """环境配置（cases.yaml 的 `meta.env`）：这份用例文件的**初始状态声明**。

    一个 cases.yaml 一份，所以一次 run 的环境只有一种；内核的槽位池照旧复用。
    「环境按用例文件走」是说初始状态随文件走，不是「每条用例一份不同环境」。
    """

    copy: list[str] = field(default_factory=list)    # 必复制（提示词等），空 = 非法
    extra: list[str] = field(default_factory=list)   # 可选；空 = 不复制
    db: DbConfig | None = None                       # None = 环境里不建库


@dataclass
class Meta:
    """cases.yaml 的大类级信息（本大类全部用例共享）。"""

    id: str
    env: EnvConfig
    name: str = ""
    dataset_version: int = 1
    premise: str = ""
    expectation: str = ""
    multi_turn: bool = False
    data: str = ""
    grading: str = ""
    source: str = ""


@dataclass
class Case:
    """一条用例。"""

    id: str
    type: str
    turns: list[Turn]
    evidence: list[str]          # 判定时取证据的位置（数据表 / 文件），可多选
    rubric: str                  # 交给裁判的判分要点
    precondition: Precondition = field(default_factory=Precondition)
    multi_turn: bool = False
    input_mode: str = INPUT_MODE_SCRIPTED
    simulator: Simulator | None = None
    judge: Judge = field(default_factory=Judge)

    @property
    def uses_simulator(self) -> bool:
        """是否需要模拟用户 agent。"""
        return self.input_mode == INPUT_MODE_AGENT

    @property
    def uses_judge(self) -> bool:
        """是否需要裁判 agent。"""
        return self.judge.mode == JUDGE_MODE_MODEL


@dataclass
class CaseSet:
    """一个 cases.yaml 的完整内容。"""

    meta: Meta
    cases: list[Case]
    notes: list[str] = field(default_factory=list)
    path: str = ""
