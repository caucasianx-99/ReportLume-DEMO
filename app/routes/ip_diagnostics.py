"""Opt-in, header-authenticated proxy diagnostics for controlled staging.

The endpoint is registered in every environment but returns 404 unless
explicitly enabled. APP_ENV and production release flags do not enable it.
Never send the secret in a URL or write it to logs.
"""

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import settings


router = APIRouter()

_SECRET_HEADER = "x-reportlume-diagnostic-key"

_RESPONSE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Robots-Tag": "noindex, nofollow",
}


@router.get("/internal/ip-diagnostics", include_in_schema=False)
async def ip_diagnostics(request: Request) -> JSONResponse:
    """Return just the three proxy identity fields on authorized requests."""

    secret = getattr(settings, "ip_diagnostics_secret", "")
    enabled = getattr(settings, "ip_diagnostics_enabled", False) is True

    if not enabled or not isinstance(secret, str) or len(secret) < 32:
        return JSONResponse(
            {"detail": "Not found"},
            status_code=404,
            headers=_RESPONSE_HEADERS,
        )

    supplied_values = request.headers.getlist(_SECRET_HEADER)

    if (
        len(supplied_values) != 1
        or len(supplied_values[0]) > 256
        or not hmac.compare_digest(supplied_values[0], secret)
    ):
        return JSONResponse(
            {"detail": "Unauthorized"},
            status_code=401,
            headers=_RESPONSE_HEADERS,
        )

    return JSONResponse(
        {
            "x_real_ip": request.headers.getlist("x-real-ip"),
            "connection_host": request.client.host if request.client else None,
            "x_forwarded_for": request.headers.get("x-forwarded-for"),
        },
        headers=_RESPONSE_HEADERS,
    )
