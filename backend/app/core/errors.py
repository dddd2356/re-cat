"""공개 API 오류 계약.

도메인·저장소 계층은 FastAPI 예외를 만들지 않는다. HTTP 경계에서만 이 모듈의
``AppError``를 응답으로 바꾼다. 원인 예외와 사용자 입력 원문은 로그에 넣지 않는다.
"""
from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    AUTH_INVALID = "AUTH_INVALID"
    AUTH_FORBIDDEN = "AUTH_FORBIDDEN"
    REPO_NOT_FOUND = "REPO_NOT_FOUND"
    REPO_FORBIDDEN = "REPO_FORBIDDEN"
    REPO_PROTECTED = "REPO_PROTECTED"
    REQUEST_INVALID = "REQUEST_INVALID"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    UPLOAD_REJECTED = "UPLOAD_REJECTED"
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
    ANALYSIS_UNAVAILABLE = "ANALYSIS_UNAVAILABLE"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    DELETION_PENDING = "DELETION_PENDING"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AppError(Exception):
    """안전한 공개 메시지와 HTTP 상태를 가진 업무 오류."""

    def __init__(self, code: ErrorCode, status_code: int, message: str):
        self.code = code
        self.status_code = status_code
        self.message = message
        super().__init__(f"{code}: {message}")


def error_body(code: ErrorCode, message: str, request_id: str) -> dict:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def code_for_status(status_code: int) -> ErrorCode:
    return {
        400: ErrorCode.REQUEST_INVALID,
        401: ErrorCode.AUTH_INVALID,
        403: ErrorCode.AUTH_FORBIDDEN,
        404: ErrorCode.REPO_NOT_FOUND,
        422: ErrorCode.VALIDATION_FAILED,
        503: ErrorCode.ANALYSIS_UNAVAILABLE,
    }.get(status_code, ErrorCode.INTERNAL_ERROR)
