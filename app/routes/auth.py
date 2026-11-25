"""Firebase email/password authentication routes."""

import logging
import time

from datetime import timedelta
from urllib.parse import parse_qs

import httpx

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from firebase_admin import auth
from starlette.concurrency import run_in_threadpool

from app.core.config import settings

from app.core.csrf import (
    attach_csrf_cookie,
    csrf_token_for,
    verify_csrf,
)

from app.core.firebase import get_firebase_app

from app.core.firestore_rest import (
    FirestoreUnavailable,
    TenantStore,
)

from app.core.rate_limit import check_auth_rate
from app.core.release_policy import account_creation_ready
from app.routes.public import templates

from app.services.dashboard_overview import (
    dashboard_summary,
)


router = APIRouter()
LOGGER = logging.getLogger(__name__)

AUTH_BASE = "https://identitytoolkit.googleapis.com/v1"


class IdentityRejected(Exception):
    pass


class IdentityUnavailable(Exception):
    pass


async def identity_request(endpoint: str, payload: dict):
    """Call a fixed Firebase Identity Toolkit operation."""

    allowed = {
        "accounts:signUp",
        "accounts:signInWithPassword",
        "accounts:sendOobCode",
    }

    if endpoint not in allowed:
        raise ValueError(
            "Unsupported authentication operation"
        )

    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(
                f"{AUTH_BASE}/{endpoint}",
                params={
                    "key": settings.firebase_web_api_key,
                },
                json=payload,
            )

    except httpx.RequestError as exc:
        raise IdentityUnavailable() from exc

    if response.status_code >= 500:
        raise IdentityUnavailable()

    if response.status_code != 200:
        raise IdentityRejected()

    try:
        return response.json()

    except ValueError as exc:
        raise IdentityUnavailable() from exc


async def read_form(request: Request) -> dict:
    """Read a small URL-encoded form with a hard byte limit."""

    content_type = request.headers.get(
        "content-type",
        "",
    ).split(";")[0].strip().lower()

    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(
            415,
            "Unsupported form type",
        )

    body = bytearray()

    async for chunk in request.stream():
        body.extend(chunk)

        if len(body) > 8192:
            raise HTTPException(
                413,
                "Form is too large",
            )

    try:
        parsed = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            max_num_fields=24,
            encoding="utf-8",
            errors="strict",
        )

    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(
            400,
            "Invalid form",
        ) from exc

    data = {
        key: values[-1]
        for key, values in parsed.items()
    }

    verify_csrf(
        request,
        data.get("csrf_token", ""),
    )

    return data


def auth_page(
    request: Request,
    mode: str,
    message: str = "",
    status_code: int = 200,
    email: str = "",
):
    token = csrf_token_for(request)

    response = templates.TemplateResponse(
        request,
        "auth.html",
        {
            "request": request,
            "settings": settings,
            "mode": mode,
            "message": message,
            "email": email,
            "csrf_token": token,
        },
        status_code=status_code,
    )

    attach_csrf_cookie(
        response,
        token,
    )

    response.headers["Cache-Control"] = "no-store"

    return response


def provider_available() -> bool:
    return settings.firebase_ready


async def ensure_admin_available():
    if not provider_available():
        raise IdentityUnavailable()

    try:
        return await run_in_threadpool(
            get_firebase_app
        )

    except Exception as exc:
        raise IdentityUnavailable() from exc


def valid_email(email: str) -> bool:
    return (
        3 <= len(email) <= 254
        and email.count("@") == 1
        and "." in email.split("@", 1)[1]
        and not any(
            char.isspace()
            for char in email
        )
    )


@router.get(
    "/signup",
    response_class=HTMLResponse,
)
async def signup_page(request: Request):
    if not account_creation_ready(settings):
        return auth_page(request, "notice", PRELAUNCH_MESSAGE)

    return auth_page(
        request,
        "signup",
    )


# Production before its privacy, legal and security approvals: no new
# accounts, so no e-mail addresses or client names are collected.
PRELAUNCH_MESSAGE = (
    "Reportlume is not open for new accounts yet. "
    "You can explore the fictional example report in the meantime."
)


@router.post(
    "/signup",
    response_class=HTMLResponse,
)
async def signup(request: Request):
    data = await read_form(request)

    if not account_creation_ready(settings):
        return auth_page(
            request, "notice", PRELAUNCH_MESSAGE, status_code=503,
        )

    await check_auth_rate(
        request,
        "signup",
    )

    email = data.get(
        "email",
        "",
    ).strip().lower()

    password = data.get(
        "password",
        "",
    )

    if (
        not valid_email(email)
        or not 12 <= len(password) <= 128
    ):
        return auth_page(
            request,
            "signup",
            "Enter a valid email and a password of "
            "12 to 128 characters.",
            status_code=400,
            email=email,
        )

    try:
        await ensure_admin_available()

        created = await identity_request(
            "accounts:signUp",
            {
                "email": email,
                "password": password,
                "returnSecureToken": True,
            },
        )

        await identity_request(
            "accounts:sendOobCode",
            {
                "requestType": "VERIFY_EMAIL",
                "idToken": created["idToken"],
            },
        )

    except IdentityRejected:
        return auth_page(
            request,
            "signup",
            "Registration could not be completed. "
            "Check the details or try signing in.",
            status_code=400,
        )

    except (IdentityUnavailable, KeyError):
        return auth_page(
            request,
            "notice",
            "Registration or verification email delivery "
            "could not be confirmed. If an account was "
            "created, use Sign in to request verification.",
            status_code=503,
        )

    return auth_page(
        request,
        "notice",
        "Check your email for the verification link. "
        "After verifying, return to Sign in.",
    )


@router.get(
    "/login",
    response_class=HTMLResponse,
)
async def login_page(request: Request):
    return auth_page(
        request,
        "login",
    )


@router.post(
    "/login",
    response_class=HTMLResponse,
)
async def login(request: Request):
    data = await read_form(request)

    email = data.get(
        "email",
        "",
    ).strip().lower()

    await check_auth_rate(
        request,
        "login",
        email=email,
    )

    password = data.get(
        "password",
        "",
    )

    if not valid_email(email) or not password:
        return auth_page(
            request,
            "login",
            "Invalid email or password.",
            status_code=400,
        )

    try:
        firebase_app = await ensure_admin_available()

        result = await identity_request(
            "accounts:signInWithPassword",
            {
                "email": email,
                "password": password,
                "returnSecureToken": True,
            },
        )

        id_token = result["idToken"]

        claims = await run_in_threadpool(
            auth.verify_id_token,
            id_token,
            app=firebase_app,
            check_revoked=True,
        )

        if not claims.get("email_verified"):
            try:
                await identity_request(
                    "accounts:sendOobCode",
                    {
                        "requestType": "VERIFY_EMAIL",
                        "idToken": id_token,
                    },
                )

            except (
                IdentityRejected,
                IdentityUnavailable,
            ):
                pass

            return auth_page(
                request,
                "notice",
                "Your email is not verified. "
                "A verification message was requested. "
                "Check your inbox before signing in.",
            )

        auth_time = claims.get(
            "auth_time",
            0,
        )

        elapsed = (
            time.time() - float(auth_time)
        )

        if not 0 <= elapsed < 300:
            return auth_page(
                request,
                "login",
                "Please sign in again.",
                status_code=401,
            )

        verified_uid = claims["uid"]

        verified_email = claims.get(
            "email",
            email,
        )

        store = TenantStore(
            verified_uid
        )

        await run_in_threadpool(
            store.ensure_profile,
            verified_email,
        )

        session_cookie = await run_in_threadpool(
            auth.create_session_cookie,
            id_token,
            expires_in=timedelta(
                days=settings.session_days
            ),
            app=firebase_app,
        )

    except IdentityRejected:
        return auth_page(
            request,
            "login",
            "Invalid email or password.",
            status_code=401,
        )

    except (
        IdentityUnavailable,
        FirestoreUnavailable,
        KeyError,
        ValueError,
    ):
        return auth_page(
            request,
            "login",
            "Authentication is temporarily unavailable.",
            status_code=503,
        )

    except Exception:
        return auth_page(
            request,
            "login",
            "Authentication could not be completed.",
            status_code=503,
        )

    response = RedirectResponse(
        "/dashboard",
        status_code=303,
    )

    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_cookie,
        max_age=settings.session_days * 86400,
        path="/",
        secure=settings.production,
        httponly=True,
        samesite="lax",
    )

    response.headers["Cache-Control"] = "no-store"

    return response


@router.get(
    "/forgot-password",
    response_class=HTMLResponse,
)
async def forgot_password_page(request: Request):
    return auth_page(
        request,
        "reset",
    )


@router.post(
    "/forgot-password",
    response_class=HTMLResponse,
)
async def forgot_password(request: Request):
    data = await read_form(request)

    email = data.get(
        "email",
        "",
    ).strip().lower()

    await check_auth_rate(
        request,
        "reset",
        email=email,
    )

    if not valid_email(email):
        return auth_page(
            request,
            "reset",
            "Enter a valid email address.",
            status_code=400,
        )

    if not provider_available():
        return auth_page(
            request,
            "reset",
            "Password reset is not configured yet.",
            status_code=503,
        )

    try:
        await identity_request(
            "accounts:sendOobCode",
            {
                "requestType": "PASSWORD_RESET",
                "email": email,
            },
        )

    except IdentityRejected:
        # Avoid disclosing whether an account exists.
        pass

    except IdentityUnavailable:
        return auth_page(
            request,
            "reset",
            "Password reset is temporarily unavailable.",
            status_code=503,
        )

    return auth_page(
        request,
        "notice",
        "If an account exists for that email, "
        "password reset instructions have been requested.",
    )


async def current_user(request: Request):
    cookie = request.cookies.get(
        settings.session_cookie_name
    )

    if not cookie or not provider_available():
        return None

    try:
        firebase_app = await run_in_threadpool(
            get_firebase_app
        )

        claims = await run_in_threadpool(
            auth.verify_session_cookie,
            cookie,
            app=firebase_app,
            check_revoked=True,
        )

        if not claims.get("email_verified"):
            return None

        return claims

    except (
        auth.InvalidSessionCookieError,
        auth.ExpiredSessionCookieError,
        auth.RevokedSessionCookieError,
        auth.UserDisabledError,
        ValueError,
    ):
        return None


@router.get(
    "/dashboard",
    response_class=HTMLResponse,
)
async def dashboard(request: Request):
    """Render a bounded summary of clients from the verified account only."""

    try:
        claims = await current_user(request)

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Authentication service unavailable",
        )

    if claims is None:
        return RedirectResponse(
            "/login",
            status_code=303,
        )

    try:
        store = TenantStore(
            claims["uid"]
        )

        overview = await run_in_threadpool(
            dashboard_summary,
            store,
        )

    except (
        FirestoreUnavailable,
        ValueError,
        KeyError,
    ):
        raise HTTPException(
            status_code=503,
            detail="Account data unavailable",
        )

    token = csrf_token_for(request)

    response = templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "settings": settings,
            "profile": overview["profile"],
            "overview": overview,
            "signed_in": True,
            "csrf_token": token,
        },
    )

    attach_csrf_cookie(
        response,
        token,
    )

    response.headers["Cache-Control"] = "no-store"

    return response


async def revoke_current_session(request: Request) -> None:
    """End the signed-in session on the server, not only in this browser.

    Firebase revokes every session of the account, so a copied cookie stops
    working too. Sign-out must still complete when Firebase is unreachable.
    """
    cookie = request.cookies.get(
        settings.session_cookie_name
    )

    if not cookie or not provider_available():
        return

    try:
        firebase_app = await run_in_threadpool(
            get_firebase_app
        )

        claims = await run_in_threadpool(
            auth.verify_session_cookie,
            cookie,
            app=firebase_app,
            check_revoked=False,
        )

        uid = claims.get("uid")

        if isinstance(uid, str) and uid:
            await run_in_threadpool(
                auth.revoke_refresh_tokens,
                uid,
                app=firebase_app,
            )

    except Exception as exc:  # noqa: BLE001 - never block sign-out
        LOGGER.warning(
            "Session revocation at sign-out failed: %s",
            type(exc).__name__,
        )


@router.post("/logout")
async def logout(request: Request):
    await read_form(request)

    await revoke_current_session(request)

    response = RedirectResponse(
        "/login",
        status_code=303,
    )

    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        secure=settings.production,
        httponly=True,
        samesite="lax",
    )

    response.headers["Cache-Control"] = "no-store"

    return response
