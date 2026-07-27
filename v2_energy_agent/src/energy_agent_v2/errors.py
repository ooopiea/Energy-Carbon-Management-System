from __future__ import annotations


class AppError(Exception):
    """业务可识别错误，携带 code 与可序列化 details。"""

    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
