"""Reportlume FastAPI application and global safety handlers."""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.headers import SecurityHeadersMiddleware
from app.core.http_errors import (
    http_exception_handler,
    unexpected_exception_handler,
    validation_exception_handler,
)

from app.routes.auth import router as auth_router
from app.routes.public import router as public_router
from app.routes.clients import router as clients_router
from app.routes.imports import router as imports_router
from app.routes.classification import router as classification_router
from app.routes.proof_of_work import router as proof_router
from app.routes.reports import router as reports_router
from app.routes.report_preview import router as report_preview_router
from app.routes.portal import router as portal_router
from app.routes.branding import router as branding_router
from app.routes.privacy_tools import router as privacy_router
from app.routes.ip_diagnostics import router as ip_diagnostics_router


LOGGER = logging.getLogger("reportlume")


app = FastAPI(
    title=settings.app_name,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(SecurityHeadersMiddleware)

app.add_exception_handler(
    StarletteHTTPException,
    http_exception_handler,
)

app.add_exception_handler(
    RequestValidationError,
    validation_exception_handler,
)

app.add_exception_handler(
    Exception,
    unexpected_exception_handler,
)

app.mount(
    "/static",
    StaticFiles(
        directory=str(
            Path(__file__).resolve().parent / "static"
        )
    ),
    name="static",
)

app.include_router(public_router)
app.include_router(auth_router)
app.include_router(clients_router)
app.include_router(imports_router)
app.include_router(classification_router)
app.include_router(proof_router)
app.include_router(reports_router)
app.include_router(report_preview_router)
app.include_router(portal_router)
app.include_router(branding_router)
app.include_router(privacy_router)
app.include_router(ip_diagnostics_router)

@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "application": "reportlume",
            "checks": "process_only",
        }
    )


@app.get("/ready", include_in_schema=False)
async def ready() -> JSONResponse:
    return JSONResponse({"status": "ok", "storage": "sqlite", "accounts_required": False}, headers={"Cache-Control": "no-store"})
