"""项目业务异常定义。

所有业务异常统一继承自 infra 层的 MyAgentError，便于上层用
`except MyAgentError` 做统一兜底。

一级域按"错误在哪条路径上被产生或终结"划分，共三个：

- `ToolExecuteError`    工具执行路径（注册 / 参数校验）
- `AgentPolicyError`    循环自身的策略判定路径（步数上限、重试耗尽、工具熔断）
- `AgentUnclaimedError` 无人认领（兜底，不表示任何路径）

域内二级按原因细分。类型只表达"来源/归属"，不表达"如何恢复"。
注意：LLM 调用路径的异常分类树与重试策略已移出内核（见 lifeprismevalue.llm）。
"""
from myagent.infra.exception import MyAgentError


# ---------------- 域：工具执行路径 ----------------

class ToolExecuteError(MyAgentError):
    """工具调用错误的父类。

    __cause__ 保留工具抛出的原始异常，供上层排查。
    """

class ToolValueError(ToolExecuteError):
    """工具注册/注销时传入非法输入。

    典型场景：tools 为 None 或类型不对、列表中混入非 Tool/非 str 元素、
    工具的 name / description / parameters 为空。
    """

class ToolValidateParameterError(ToolExecuteError):
    """模型传入的工具参数未通过 schema 校验。

    典型场景：缺少 required 中声明的字段、参数类型与 schema 声明不符。
    """

# ---------------- 域：循环自身的策略判定 ----------------

class AgentPolicyError(MyAgentError):
    """agent 循环自身的策略判定错误（来源：循环的编排/预算机制）。

    与另两个域的区别：本域不是"外部依赖失败了"，而是"循环按自己的配置/预算
    判定应当终止"——触发条件完全由我方确定，因此本域成员可枚举、判据唯一。

    __cause__ 可选：若该判定由某个底层错误触发（如重试上限来自最后一次 LLM
    错误），用 `raise ... from` 挂上，根因由异常链承载；本类型只表达"哪个策略
    判定终止了执行"。
    """

class MaxStepsExceededError(AgentPolicyError):
    """达到本 turn 的最大步数上限，强制终止。

    典型场景：模型反复调用工具或多轮不收敛，触达 agent_config.step_limit。
    由 loop 在步数预算耗尽时抛出。
    """

class RetryExhaustedError(AgentPolicyError):
    """重试次数达到上限，不再重试。

    典型场景：同一 LLM 调用连续失败，重试次数超过 agent_config.max_retry_count。
    由 loop 的重试处理器产出，并以 `from` 挂上最后一次失败的错误。

    注意：单次失败的原因不在本类型上体现，而在 __cause__ 链中（耗尽那次未重试，故不落
    llm/retry 记录；其错误另由 step/end 与 turn/end 的 reason_text 承载）；本类型只表达
    "重试策略已耗尽"这一终态。
    """

class ToolConsecutiveFailureError(AgentPolicyError):
    """单个工具连续失败达到熔断阈值，且该工具配置了熔断即抛错（人在回路入口）。

    由 ToolRegister.execute 在触发熔断时抛出（阈值属循环配置）；经 step 内 TaskGroup 以
    ExceptionGroup 形式上抛，由 loop 接住：记录 request/error 后终止本
    turn（中断当前行为）。与"功能降级"（返回带 hint 的错误结果、agent
    继续运行）相对，本类型表示该工具已不可用、需要外部介入。
    """

# ---------------- 域：无人认领（兜底） ----------------

class AgentUnclaimedError(MyAgentError):
    """兜底：异常已被捕获，但没有任何订阅方认领它，循环无法继续。

    本类型不表示某条路径，只表示"这次失败没有处置方"，因此不能当作推断来源的
    依据；来源要看 `__cause__` 链。典型承接场景：

    - 策略表没有覆盖的错误（如 401 认证失败这类已从策略表移除的类别）；
    - 循环自身的策略判定（如步数上限）——判定由我方做出，但同样无人认领；
    - 编排层冒出的、不属任何已知类型的异常（如 TaskGroup 中未归因的异常）。

    与之相对：异常若已有处置方（如已按 retry 策略接住），应按原类型抛出，
    不要包成本类型。

    —— 本类型由原 `LLmError` 改名而来 ——
    """


class SessionReBindError(MyAgentError):
    """
    session绑定eventservice时重复绑定事件总线
    """