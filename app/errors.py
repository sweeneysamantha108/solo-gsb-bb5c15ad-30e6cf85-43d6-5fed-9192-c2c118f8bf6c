from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def not_found(resource: str, key: str) -> ApiError:
    return ApiError(404, "not_found", f"{resource} not found: {key}")


def conflict(code: str, message: str, details=None) -> ApiError:
    return ApiError(422, code, message, details)


async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    body = {"error": {"code": exc.code, "message": exc.message}}
    if exc.details:
        body["error"]["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=body)
