from dataclasses import dataclass
from typing import Literal

@dataclass
class HumanChoice:
    choice_name : str # 选项名称/展示名称
    choice_id : str # 选项本体id
    description : str = "" # 该选项的解释

@dataclass
class HumanReturn:
    choice_id : str|list[str] # 返回的选项
    content : str # 附加的信息


@dataclass
class HITLMessage:
    human_return_type : Literal["message-only","single-select","multiple-select"]
    # 类型说明：human_return_type，是指当前希望人类返回的消息是什么
    # message-only ： 只返回文字。(当前这个只是预留空位，未来可能会需要，但是目前占时不需要)
    # single-select : 单选，比如从pass和deny中进行选择
    # multiple-select ：多选 
    # 未来可以扩充到：单选+message（补充说明的消息），或多选+message
    content : str # 请求说明
    choices : list[HumanChoice] # 选项内容