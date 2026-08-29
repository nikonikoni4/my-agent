from myagent.agent.core import Tool
from myagent.agent.execption import ToolValueError,ToolExecuteError
import logging 
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
class ToolRegister:
    def __init__(self,):
        self._tools = {}
        

    def register(self,tools : Tool | list[Tool]):
        """
        注册工具
        args:
            Tools : 单个工具或一起注册的工具列表
        """
        # 检查输入
        if tools is None or not (isinstance(tools, Tool) or isinstance(tools, list)):
            raise ToolValueError("注册工具为None 或 非tools类型")
        if isinstance(tools, Tool):
            tools = [tools]

        # 注册工具
        for tool in tools:
            if not isinstance(tool, Tool):
                raise ToolValueError(f"列表中存在非 Tool 元素: {type(tool)}")
            if not tool.description or not tool.name or not tool.parameters:
                raise ToolValueError("工具描述或名称或参数为空")
            if tool.name in self._tools :
                logger.warning(f"工具{tool.name}重复注册或有同名tool")
                continue
            self._tools[tool.name] = tool

    def unregister(self,tools:str|list[str]):
        """
        注销工具
        args:
            Tools : 单个工具或一起注册的工具列表
        """
        # 检查输入
        if tools is None or not (isinstance(tools, str) or isinstance(tools, list)):
            raise ToolValueError(f"注销输入参数错误{type(tools)},正确应该是str | list(str)")
        if isinstance(tools,str):
            tools = [tools]

        for tool in tools:
            if not isinstance(tool, str):
                raise ToolValueError(f"列表中存在非 str 元素: {type(tool)}")
            self._tools.pop(tool,None)

    def tool_list(self)->list[str]:
        """获取已经注册的tool"""
        return list(self._tools.keys())

    def execute(self,tool_name,**kwargs)->str:
        """执行工具"""
        if tool_name not in self._tools:
            logger.warning(f"{tool_name}工具不存在/未注册")
            return f"status : error \n message : {tool_name}工具不存在 \n hint : 可用工具 {','.join(self.tool_list())} "
        # 工具参数校验
        # pass

        try:
            return self._tools[tool_name].execute(**kwargs)
        except Exception as e:
            logger.error(f"{tool_name}工具调用错误，参数:{kwargs}")
            raise ToolExecuteError(f"{tool_name}工具调用错误，参数:{kwargs}")