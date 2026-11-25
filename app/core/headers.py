"""Security response headers and private-workflow cache policy."""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.config import settings


PRIVATE_PREFIXES = (
    "/clients",
    "/api/",
    "/r/",
    "/case-consent/",
    "/cases/",
    "/settings/",
    "/billing",
    "/internal/",
)

PRIVATE_EXACT = {
    "/login",
    "/signup",
    "/logout",
    "/forgot-password",
    "/dashboard",
}

NOINDEX_PREFIXES = PRIVATE_PREFIXES + (
    "/sample",
)

# Stylesheets, scripts and images keep their URL when they change, so the
# browser must check them with the server (ETag) before reusing a copy.
STATIC_PREFIX = "/static/"

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply browser security headers to all application responses."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"

        response.headers["Referrer-Policy"] = "no-referrer"

        response.headers["X-Frame-Options"] = "DENY"

        policy = {
            "default-src": ["'self'"],
            "base-uri": ["'self'"],
            "object-src": ["'none'"],
            "frame-ancestors": ["'none'"],
            "form-action": ["'self'"],
            "img-src": ["'self'", "data:"],
            "style-src": ["'self'"],
            "script-src": ["'self'"],
            "connect-src": ["'self'"],
        }

        response.headers["Content-Security-Policy"] = "; ".join(
            f"{name} {' '.join(values)}" for name, values in policy.items()
        )

        response.headers["Permissions-Policy"] = (
            "camera=(), "
            "microphone=(), "
            "geolocation=()"
        )

        path = request.url.path

        private = (
            path in PRIVATE_EXACT
            or path.startswith(PRIVATE_PREFIXES)
        )

        noindex = (
            private
            or path.startswith(NOINDEX_PREFIXES)
        )

        if private:
            response.headers["Cache-Control"] = "no-store"
        elif path.startswith(STATIC_PREFIX):
            response.headers["Cache-Control"] = "no-cache"

        if noindex:
            response.headers["X-Robots-Tag"] = (
                "noindex, nofollow"
            )

        if settings.production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000"
            )

        return response
