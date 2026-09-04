"""RFC 9457 problem+json errors with stable domain codes.

The frontend branches on `code`, never on prose, so error handling survives
message rewording.
"""
from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

CONTENT_TYPE = "application/problem+json"


class DomainError(Exception):
    """Base for errors that carry a stable machine-readable code."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "domain_error"
    title = "Request could not be processed"

    def __init__(self, detail: str = "", *, code: str | None = None,
                 status_code: int | None = None, **meta) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.meta = meta


class NotFound(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    title = "Resource not found"


class Unauthorized(DomainError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"
    title = "Authentication required"


class StagePreconditionFailed(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "stage_precondition_failed"
    title = "The project is not in a state that allows this"


class BudgetExceeded(DomainError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "budget_exceeded"
    title = "This would exceed the project budget"


def _problem(status_code: int, code: str, title: str, detail: str,
             instance: str, **meta) -> JSONResponse:
    body = {"type": f"https://hbday-zee.local/errors/{code}", "title": title,
            "status": status_code, "detail": detail, "code": code,
            "instance": instance}
    if meta:
        body["meta"] = jsonable_encoder(meta)
    return JSONResponse(body, status_code=status_code,
                        media_type=CONTENT_TYPE)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError):
        return _problem(exc.status_code, exc.code, exc.title, exc.detail,
                        str(request.url.path), **exc.meta)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return _problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_failed",
            "Request body failed validation",
            f"{len(exc.errors())} field(s) rejected",
            str(request.url.path), errors=exc.errors())

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return _problem(exc.status_code, f"http_{exc.status_code}",
                        "Request failed", str(exc.detail),
                        str(request.url.path))
