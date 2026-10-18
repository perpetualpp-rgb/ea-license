"""JSON error envelope: {"error": {"code": "...", "message": "...", "details": {...}}}.

Error bodies never echo credentials. Another member's record is reported as 404, not
403, so ids cannot be probed.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("flowlab.errors")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Optional[Dict[str, Any]] = None,
                 headers: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.details = details or {}
        self.headers = headers


def not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} not found")


def body(code: str, message: str, details: Optional[Dict[str, Any]] = None) -> dict:
    err: Dict[str, Any] = {"code": code, "message": message}
    if details:
        err["details"] = details
    return {"error": err}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return JSONResponse(body(exc.code, exc.message, exc.details), status_code=exc.status, headers=exc.headers)

    @app.exception_handler(psycopg.errors.UniqueViolation)
    async def _unique(_: Request, exc: psycopg.errors.UniqueViolation):
        return JSONResponse(body("conflict", "a record with the same unique key already exists",
                                 {"constraint": getattr(exc.diag, "constraint_name", None)}), status_code=409)

    @app.exception_handler(psycopg.errors.CheckViolation)
    async def _check(_: Request, exc: psycopg.errors.CheckViolation):
        return JSONResponse(body("validation_error", "value rejected by a database constraint",
                                 {"constraint": getattr(exc.diag, "constraint_name", None)}), status_code=422)

    @app.exception_handler(psycopg.errors.NotNullViolation)
    async def _not_null(_: Request, exc: psycopg.errors.NotNullViolation):
        return JSONResponse(body("validation_error", "a required value is missing",
                                 {"column": getattr(exc.diag, "column_name", None)}), status_code=422)

    @app.exception_handler(psycopg.errors.ForeignKeyViolation)
    async def _fk(_: Request, exc: psycopg.errors.ForeignKeyViolation):
        return JSONResponse(body("validation_error", "referenced record does not exist",
                                 {"constraint": getattr(exc.diag, "constraint_name", None)}), status_code=422)

    @app.exception_handler(psycopg.errors.InsufficientPrivilege)
    async def _priv(_: Request, exc: psycopg.errors.InsufficientPrivilege):
        log.warning("database refused a statement (privilege/RLS): %s", type(exc).__name__)
        return JSONResponse(body("forbidden", "not allowed"), status_code=403)

    @app.exception_handler(psycopg.errors.RestrictViolation)
    async def _immutable(_: Request, exc: psycopg.errors.RestrictViolation):
        return JSONResponse(body("immutable", "this record cannot be changed"), status_code=409)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        # type only: exception text can carry SQL parameters (member data) into logs
        log.error("unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path)
        return JSONResponse(body("internal_error", "unexpected server error"), status_code=500)

    @app.exception_handler(psycopg.OperationalError)
    async def _db_down(_: Request, exc: psycopg.OperationalError):
        log.error("database unavailable: %s", type(exc).__name__)
        return JSONResponse(body("database_unavailable", "database unavailable, retry later"), status_code=503)
