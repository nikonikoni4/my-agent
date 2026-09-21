# system prompt 需要实现
# 1. 隔离机制：每个agent的systemprompt加载需要隔离，实现方法： 每个agent内传入system prompt 对象，进行显示注册，agent_name -> prompt
# 2. 遮蔽机制：若agent注册的promptSection名称相同，则全局默认的该section被覆盖
# 3. 实现动态变化 ： 例子
#    1） 不同agent注册不同的工具 -》 工具说明的systemprompt也会跟着变化

from myagent.agent.core.systemprompt.types import PrompSection,AssemblyPrompt,ContextItem,ContextType
from collections import defaultdict
from typing import Callable
import  logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
class SystemPrompt:
    def __init__(self):
        self._global_prompt = self._init_global_prompt()
        self._agent_prompt: dict[str, dict[str, PrompSection]] = defaultdict(dict)  # agent_name -> name -> section
        # agent_name -> 带类型标注的上下文条目（System Reminder / Runtime）
        self._agent_context: dict[str, list[ContextItem]] = defaultdict(list)
    def _init_global_prompt(self)->dict[str,PrompSection]:
        """
        默认系统提示词
        return
            dict[str,PrompSection] name -> prompt section
        暂时写100
        """
        identity = PrompSection(
            name = "identity",
            order = -100, # 排序含义：最先读，非保护级别
            text = "你是一个个人助手,你的根本职责是帮助用户完成完成各种任务"
        )
        return {identity.name: identity}

    def register_section(self,agent_name,prompt_section:PrompSection):
        """
        显式注册某个agent独有的提示词；同名 section 覆盖（遮蔽），全球层保持不变
        args:
            agent_name : agent名称
            prompt_section : 提示词
        """
        logger.info(f"注册{agent_name}的prompt{prompt_section.name}")
        self._agent_prompt[agent_name][prompt_section.name] = prompt_section

    def unregister(self,agent_name):
        logger.info(f"注销{agent_name}的prompt、runtime context 与 system reminder")
        self._agent_prompt.pop(agent_name, None)
        self._agent_context.pop(agent_name, None)


    def register_context(self,agent_name,context:str|Callable,name:str|None = None):
        """
        为指定 agent 注册 Runtime-context（运行过程中动态生成的标注，如当前时间、
        终端位置等）。该内容不进 System Prompt（避免破坏前缀缓存），assemble 后
        合并进当前传入消息的 UserMessage。
        args:
            agent_name : agent名称
            context : runtime-context 文本；也可传 callback 做参数注入（见 name）
            name : 参数注入键，对应 assemble 的 params 外层键。仅当 context 是
                callback（签名 render(**params) -> str）时才有意义
        """
        logger.info(f"注册{agent_name}的runtime context")
        self._agent_context[agent_name].append(
            ContextItem(text=context, context_type=ContextType.RUNTIME, name=name)
        )

    def register_system_reminder(self,agent_name,reminder:str|Callable,name:str|None = None):
        """
        为指定 agent 注册 System Reminder。主要来源于文件读取，也允许在系统中
        动态添加。assemble 后作为 Message List 的第二位（System Prompt 之后、
        正常对话之前）。
        args:
            agent_name : agent名称
            reminder : system-reminder 文本；也可传 callback 做参数注入（见 name）。
                读文件得来的提醒常含绝对路径，包成 callback 后由 assemble 按
                data_path 注入，避免把本机路径写死在数据文件里
            name : 参数注入键，对应 assemble 的 params 外层键（如 "custom_prompt"）
        """
        logger.info(f"注册{agent_name}的system reminder")
        self._agent_context[agent_name].append(
            ContextItem(text=reminder, context_type=ContextType.SYSTEM_REMINDER, name=name)
        )
    def assemble(self,agent_name,params:dict|None = None)->AssemblyPrompt:
        """
        组装指定 agent 的提示词：
        - sections：全局 section 与 agent section 合并（同名遮蔽）
        - context：Runtime context 拼接，合并进当前 UserMessage
        - system_reminder：System Reminder 拼接，作为 Message List 第二位
        args:
            agent_name : agent名称
            params : 上下文条目的注入参数，结构同 render：{条目名: {占位符名: 值}}。
                只有注册时传了 name 且 text 是 callback 的条目吃参数；不传则所有
                条目按原文拼（str 条目与参数无关，行为不变）
        return
            AssemblyPrompt
        """
        prompt_dict = {**self._global_prompt,**self._agent_prompt[agent_name]}
        items = self._agent_context[agent_name]
        runtime_context = self._join_context(
            [i for i in items if i.context_type == ContextType.RUNTIME], params
        )
        system_reminder = self._join_context(
            [i for i in items if i.context_type == ContextType.SYSTEM_REMINDER], params
        )
        assembly_prompt = AssemblyPrompt(prompt_dict,runtime_context,system_reminder)
        # 触发system-prompt/assemble 
        # 暂时不实现
        return assembly_prompt

    @staticmethod
    def _join_context(items:list[ContextItem],params:dict|None) -> str:
        """拼接同类上下文条目：callback 条目按 name 取参数展开，str 条目原样拼。

        与 render 对 section 的处理是同一条约定，差别只在参数键的来源——section
        用段名，条目用注册时声明的 name。callback 条目缺参数时**跳过该条**（与
        callback section 缺参数时跳过整段一致），只留一条 warning，不抛异常：
        提醒少一条是可降级的观测问题，不值得把整个请求打挂。
        """
        params = params or {}
        parts: list[str] = []
        for item in items:
            if not callable(item.text):
                if item.name in params:
                    logger.warning(f"{item.name}的text不是callback类型，不能进行参数注入")
                parts.append(item.text)
                continue
            if item.name not in params:
                logger.warning(f"{item.name}的text是callback类型，但无params注入参数")
                continue
            parts.append(item.text(**params[item.name]))
        return "\n".join(parts)

    @staticmethod
    def render(assembly_prompt:AssemblyPrompt,params:dict|None = None):
        """
        渲染提示词中的变量

        args:
            assembly_prompt : assemble 的产物，决定有哪些 section、按什么顺序拼
            params : 渲染参数，结构为 **{section 名: {占位符名: 值}}** 两层 dict。
                值不是整体塞给某段，而是展开后传给该段的 callback，即等价于
                `section.text(**params[section.name])`——所以两层的 key 都由
                section 自己说了算：
                  - 外层键 = 注册时用的 section 名（register_section 的第一个参数）
                  - 内层键 = 该段正文里 {占位符} 的花括号内名字，必须与 callback
                    形参名逐字一致，否则 TypeError
                只有 callback 段（text 可调用）吃参数：非 callback 段被给了参数
                只记一条 warning、原样输出；callback 段没被给参数则整段跳过。
                例：{"agent": {"agent_path": "...", "user_path": "..."}}
        return:
            按 order 升序拼接后的 section 文本；**不含** runtime-context 与
            system-reminder（后两者在 AssemblyPrompt 上是独立字段，由调用方
            自行取用，见 assemble）
        """
        params = params or {}
        parts = []
        section_list = assembly_prompt.sorted_sections()
        for s in section_list:
            if callable(s.text):
                if s.name in params:
                    parts.append(s.text(**params[s.name]))
                else:
                    logger.warning(f"{s.name}的text是callback类型，但无params注入参数")
            else:
                if s.name in params:
                    logger.warning(f"{s.name}的text不是callback类型，不能进行参数注入")
                parts.append(s.text)
        return  "\n".join(parts)

    
