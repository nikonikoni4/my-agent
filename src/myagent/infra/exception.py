
from typing import Any


class MyAgentError(Exception):
    """项目异常基类。

    所有业务异常必须继承此类。通过 code / details 携带结构化上下文，
    原因链由异常链本身（`raise X from e` → __cause__）承载。

    注意：
    - to_dict() 用于调试和日志记录，包含完整的异常信息（error_type + cause）
    - API 响应由 api_error_mapping.map_app_error() 生成，只包含 error_code + message + details
    - 两者格式不同是设计决策：to_dict() 给开发者看，API 响应给前端看
    """

    def __init__(
        self,
        message: str | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        final_message = message or self.__class__.__name__
        self.code = code
        self.message = final_message
        self.details = details or {}
        super().__init__(final_message)

    def to_dict(self) -> dict[str, Any]:
        """将异常序列化为字典（用于调试和日志记录）。

        注意：此方法不用于生成 API 响应。API 响应格式由
        api_error_mapping.map_app_error() 统一生成。
        """
        result: dict[str, Any] = {
            "error_type": self.__class__.__name__,
            "message": self.message,
            "code": self.code,
            "details": self.details,
        }
        if self.__cause__ is not None:
            result["cause"] = str(self.__cause__)
        return result

