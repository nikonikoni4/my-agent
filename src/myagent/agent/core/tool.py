from abc import ABC,abstractmethod
import json
from typing import Any
class Tool(ABC):
    
    def __init__(self):
        pass

    @property
    @abstractmethod
    def name(self)->str:
        """
        工具名称
        """
        pass
    @property
    @abstractmethod
    def description(self)->str:
        """
        工具描述
        """
        pass
    @property
    @abstractmethod
    def parameters(self)-> dict[str, Any]:
        """
        {
            "type" : "object" , <- 第一层嵌套固定是object
            "properties" : {
                "<parameter_name>" : {
                    "type" : string / number / integer / boolean / array / object
                    "description": "...",     ← 可有，给模型的说明
                    "items": {...},           ← type 是 array 时才有
                    "enum": ["a", "b"],       ← type 是 string/integer 时可选，限定取值
                
                # example1: 单个string/integer等参数
                "parameter_name" : { 
                    "type" : "string",
                    "description" : "...",
                    "enum" : ["a","b"],
                }
                # example2:多个参数
                "parameter_name" :  {
                    "type": "object",
                    "description" : "...",
                    "properties" : {
                    }
                }

                example3: 输入字符串数组等
                "parameter_name" : {
                    "type" : "array",
                    "description" : "...",
                    "items" : { 
                        "type": "string"
                    }
                }
            }
            "required":[...]
        }
        """
        pass

    @abstractmethod
    def execute(self,)->str:
        """
        具体执行
        """
        pass 

    def validate(self):
        """参数校验"""
        pass

    def to_schema(self)->dict[str,Any]:
        return {
            "type" : "function",
            "function":{
                "name" : self.name,
                "description":self.description,
                "parameters":self.parameters
            }
        }