from myagent.infra.exception import MyAgentError

class ToolValueError(MyAgentError):
    pass 

class ToolExecuteError(MyAgentError):
    pass

class ToolValidateParameterError(MyAgentError):
    pass

class LLMCallError(MyAgent):
    pass