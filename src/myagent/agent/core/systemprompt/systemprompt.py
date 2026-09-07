# system prompt 需要实现
# 1. 隔离机制：每个agent的systemprompt加载需要隔离，实现方法： 每个agent内传入system prompt 对象，进行显示注册，agent_name -> prompt
# 2. 遮蔽机制：若agent注册的promptSection名称相同，则全局默认的该section被覆盖
# 3. 实现动态变化 ： 例子
#    1） 不同agent注册不同的工具 -》 工具说明的systemprompt也会跟着变化

from myagent.agent.core.systemprompt.types import PrompSection,AssemblyPrompt
from collections import defaultdict
import  logging 
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
class SystemPrompt:
    def __init__(self):
        self._global_prompt = self._init_global_prompt()
        self._agent_prompt: dict[str, dict[str, PrompSection]] = defaultdict(dict)  # agent_name -> name -> section
        self._agent_context: dict[str, list[str]] = defaultdict(list)  # agent_name -> [context 文本]
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
        logger.info(f"注销{agent_name}的prompt和runtime context")
        self._agent_prompt.pop(agent_name, None)
        self._agent_context.pop(agent_name, None)


    def register_context(self,agent_name,context:str):
        """
        为不同agent注册Runtime-context，该内容会整合进user message
        args:
        """
        logger.info(f"注册{agent_name}的runtime context")
        self._agent_context[agent_name].append(context)
    def assemble(self,agent_name)->AssemblyPrompt:
        """
        args:
            params : 组装prompt需要注入的参数 参数名称 ->  dict 参数
        """
        prompt_dict = {**self._global_prompt,**self._agent_prompt[agent_name]}
        runtime_context = "\n".join(self._agent_context[agent_name])
        assembly_prompt = AssemblyPrompt(prompt_dict,runtime_context)
        # 触发system-prompt/assemble 
        # 暂时不实现
        return assembly_prompt

    @staticmethod
    def render(assembly_prompt:AssemblyPrompt,params:dict|None = None):
        """
        渲染提示词中的变量
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

    
