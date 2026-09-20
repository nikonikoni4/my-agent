from dataclasses import dataclass
from typing import Protocol
from myagent.agent.hitl.types import HumanChoice,HITLMessage

    


class HITLChannel(Protocol):
    async def ask_human(message:HITLMessage):
        pass