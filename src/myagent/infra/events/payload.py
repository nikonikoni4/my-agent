from dataclasses import dataclass


@dataclass
class Payload:
    pass

class PreAgentLoopPayload(Payload):
    pass
@dataclass
class AfterLLmCallPayload(Payload):
    messages : list 
    tool : list
    response = None
    pass

class AfterToolUserPayload(Payload):
    pass