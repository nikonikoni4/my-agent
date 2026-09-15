"""数据层业务异常。

移植自 lifeprism.utils.exceptions / repository.exceptions，供工具回喂结构化错误。
"""

from __future__ import annotations

from typing import Any


class ValidationError(Exception):
    """参数校验错误，携带 code / message / details 供工具回喂模型。"""

    def __init__(self, message: str, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}

    def __str__(self) -> str:
        return self.message


class DuplicateEntityError(Exception):
    """实体唯一性冲突。"""

    def __init__(self, entity_type: str, entity_id: str, conflict_field: str | None = None):
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.conflict_field = conflict_field
        super().__init__(f"{entity_type} 已存在: {entity_id}")


class EntityNotFoundError(Exception):
    """实体不存在。"""

    def __init__(self, entity_type: str, entity_id: str):
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"{entity_type} 不存在: {entity_id}")


class DataAccessError(Exception):
    """数据库访问异常。"""

    def __init__(self, message: str, details: dict | None = None, cause: Exception | None = None):
        self.message = message
        self.details = details or {}
        self.cause = cause
        if cause is not None:
            super().__init__(f"{message}: {cause}")
        else:
            super().__init__(message)