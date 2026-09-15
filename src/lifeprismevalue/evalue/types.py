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
class Meta:
    """cases.yaml 的大类级信息（本大类全部用例共享）。"""

    id: str
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
