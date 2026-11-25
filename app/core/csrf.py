
"""Double-submit CSRF protection for HTML forms."""

import secrets

from fastapi import HTTPException, Request
from starlette.responses import Response

from app.core.config import settings


def csrf_token_for(request: Request) -> str:
    existing = request.cookies.get(
        settings.csrf_cookie_name, ""
    )

    if (
        32 <= len(existing) <= 64
        and all(
            c.isalnum() or c in "-_"
            for c in existing
        )
    ):
        return existing

    return secrets.token_urlsafe(32)


def attach_csrf_cookie(
    response: Response,
    token: str,
) -> None:
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=token,
        max_age=21600,
        path="/",
        secure=settings.production,
        httponly=True,
        samesite="lax",
    )


def verify_csrf(
    request: Request,
    submitted_token: str,
) -> None:
    cookie_token = request.cookies.get(
        settings.csrf_cookie_name, ""
    )

    if not cookie_token or not submitted_token:
        raise HTTPException(
            status_code=403,
            detail="Invalid form token",
        )

    if not secrets.compare_digest(
        cookie_token,
        submitted_token,
    ):
        raise HTTPException(
            status_code=403,
            detail="Invalid form token",
        )
