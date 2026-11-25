"""Safe browser and JSON error negotiation.

Owner pages may show strictly allowlisted actionable errors.
Customer bearer links always receive generic, brand-free error pages.
No exception text, token or submitted form value is reflected directly.
"""

import logging
from dataclasses import dataclass

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.routes.public import templates


@dataclass(frozen=True)
class OwnerFeedback:
    message: str
    billing_link: bool = False


_SAFE_DETAILS = {
    "Your trial has ended. Data is read-only.": OwnerFeedback(
        "Your 72-hour free trial has ended. Existing reports remain "
        "available to view. Review your subscription options to "
        "continue editing or publishing.",
        True,
    ),

    "Your subscription is not active.": OwnerFeedback(
        "Your subscription does not currently permit editing or "
        "publishing. Review your billing status.",
        True,
    ),

    "The one trial report has already been used.": OwnerFeedback(
        "Your free trial includes one published monthly report. "
        "You cannot use that allowance for a different client "
        "or reporting month.",
        True,
    ),

    "Trial starts after the first confirmed real CSV import.": OwnerFeedback(
        "Import and confirm your first authorized X Content CSV "
        "before publishing a free-trial report."
    ),

    "Client is outside trial allowance.": OwnerFeedback(
        "This client is outside your one-client trial allowance. "
        "Review your subscription options.",
        True,
    ),

    "This client is not covered by the free trial.": OwnerFeedback(
        "This client is not included in the free trial. "
        "Review your subscription options.",
        True,
    ),

    "A report needs completed-import posts before publishing.": OwnerFeedback(
        "This report has no eligible posts or replies from a "
        "completed CSV import. Import and confirm real data, "
        "then save a new report version."
    ),

    "Set your agency name before publishing.": OwnerFeedback(
        "Replace the placeholder agency name with your real "
        "agency or professional name, then save a new version."
    ),

    "Confirm that you want to publish an unfinished month.": OwnerFeedback(
        "This reporting month is not finished. Metrics may "
        "continue to change. Confirm that you intend to "
        "publish an interim report."
    ),

    "Confirm which month will use your one trial report.": OwnerFeedback(
        "Confirm that publishing this report will use your "
        "one free-trial report allowance for the selected month."
    ),

    "Publishing checklist confirmation is required.": OwnerFeedback(
        "Review the saved report and confirm the publishing "
        "checklist before continuing."
    ),

    "An active, verified paid subscription is required.": OwnerFeedback(
        "Publishing an unmarked copy of a trial report requires "
        "an active, verified Professional subscription.",
        True,
    ),

    "Confirm that you want to publish a new paid version.": OwnerFeedback(
        "Confirm that you want to create a new unmarked version "
        "from the currently published frozen report."
    ),

    "The published report already has no trial mark.": OwnerFeedback(
        "The published report is already unmarked. "
        "No additional copy is necessary."
    ),

    "Publish a report before copying it.": OwnerFeedback(
        "Publish the original report before requesting "
        "an unmarked copy."
    ),

    "Publish a report before sharing it.": OwnerFeedback(
        "Publish a saved report version before creating "
        "a private client link."
    ),

    "A confirmed active trial is required.": OwnerFeedback(
        "Your free trial is no longer eligible for creating "
        "a new private link. Review your subscription status.",
        True,
    ),

    "Report changed. Refresh and retry.": OwnerFeedback(
        "The report changed since you opened it. "
        "Refresh the workspace before trying again."
    ),

    "Report changed. Refresh and retry.": OwnerFeedback(
        "Another report update was saved. Refresh the workspace "
        "before trying again."
    ),

    "Publishing conflicted with another update.": OwnerFeedback(
        "The report changed during publishing. "
        "Refresh the workspace to check its current state."
    ),

    "The report was updated by another request.": OwnerFeedback(
        "Another change was saved. Refresh the workspace "
        "before trying again."
    ),

    "Report form is too large. Shorten the text and try again.": OwnerFeedback(
        "The report form exceeds the allowed size. "
        "Shorten the text before saving."
    ),

    "Invalid report form.": OwnerFeedback(
        "The report form could not be read. "
        "Check the fields and try again."
    ),

    "Duplicate report form field.": OwnerFeedback(
        "The report form contains duplicate fields. "
        "Reload the page before trying again."
    ),

    "Unsupported report form type.": OwnerFeedback(
        "This report form format is not supported. "
        "Reload the page before trying again."
    ),
}


_LIMITED_FIELDS = {
    "brand": (80, "Agency name"),
    "executive_summary": (2000, "Executive summary"),
    "next_month_plan": (1500, "Next month's plan"),
    "client_message": (1500, "Client message"),
}


_DEFAULTS = {
    400: (
        "Check the submitted information and try again."
    ),
    401: (
        "Sign in to continue."
    ),
    403: (
        "This action is unavailable for your current account access."
    ),
    404: (
        "The requested record is unavailable."
    ),
    409: (
        "Another change was saved. Refresh and try again."
    ),
    413: (
        "The submitted form is too large. Shorten it and try again."
    ),
    415: (
        "This form format is unsupported. Reload and try again."
    ),
    422: (
        "Check the entered information and try again."
    ),
    503: (
        "The service is temporarily unavailable. Try again later."
    ),
}


def owner_feedback(status_code: int, detail) -> OwnerFeedback:
    """Return only a known, safe message for an owner-side error."""

    if isinstance(detail, str):
        known = _SAFE_DETAILS.get(detail)

        if known is not None:
            return known

        if status_code in (400, 422):
            for field, (limit, label) in _LIMITED_FIELDS.items():
                if detail == (
                    f"{field} must be at most "
                    f"{limit} characters."
                ):
                    return OwnerFeedback(
                        f"{label} must be {limit:,} characters "
                        "or fewer. Your changes were not saved."
                    )

                if detail == (
                    f"{field} contains forbidden control characters."
                ):
                    return OwnerFeedback(
                        f"{label} contains unsupported control "
                        "characters. Remove them and try again."
                    )

    return OwnerFeedback(
        _DEFAULTS.get(
            status_code,
            "The requested action could not be completed.",
        )
    )


LOGGER = logging.getLogger(__name__)

PRIVATE_LINK_PREFIXES = (
    "/r/",
    "/case-consent/",
    "/cases/",
)

OWNER_PREFIXES = (
    "/clients/",
    "/dashboard",
    "/billing",
    "/settings/",
)

API_PREFIXES = (
    "/api/",
    "/webhooks/",
)

ERROR_MESSAGES = {
    400: (
        "Invalid request",
        "Check your information and try again.",
    ),
    401: (
        "Sign in required",
        "Sign in to continue to your workspace.",
    ),
    403: (
        "Action unavailable",
        "You do not have access to this action.",
    ),
    404: (
        "Page unavailable",
        "The page may have moved or no longer be available.",
    ),
    405: (
        "Method not allowed",
        "This action is not available on this page.",
    ),
    409: (
        "Changes not saved",
        "Refresh the page before trying again.",
    ),
    413: (
        "Form too large",
        "Shorten the submitted information and try again.",
    ),
    415: (
        "Unsupported form",
        "Submit the form using the supported format.",
    ),
    422: (
        "Invalid information",
        "Check the submitted fields and try again.",
    ),
    429: (
        "Too many attempts",
        "Try again later.",
    ),
    500: (
        "Service error",
        "The page could not be loaded right now.",
    ),
    503: (
        "Temporarily unavailable",
        "The service is temporarily unavailable.",
    ),
}


def is_private_link(path: str) -> bool:
    return path.startswith(
        PRIVATE_LINK_PREFIXES
    )


def is_owner_page(path: str) -> bool:
    return path.startswith(
        OWNER_PREFIXES
    )


def wants_html(request: Request) -> bool:
    """Use JSON for APIs and HTML for explicit browser navigation."""

    path = request.url.path

    if path.startswith(API_PREFIXES) or path == "/health":
        return False

    if (
        path.startswith("/static/")
        or path in (
            "/social-card.png",
            "/robots.txt",
        )
    ):
        return False

    if request.headers.get("sec-fetch-mode") == "navigate":
        return True

    accept = request.headers.get(
        "accept",
        "",
    ).lower()

    return "text/html" in accept


def no_store_headers() -> dict:
    return {
        "Cache-Control": "no-store",
        "X-Robots-Tag": "noindex, nofollow",
    }


def page_error(
    request: Request,
    status_code: int,
    detail=None,
):
    heading, message = ERROR_MESSAGES.get(
        status_code,
        ERROR_MESSAGES[500],
    )

    billing_link = False

    if is_private_link(request.url.path):
        template_name = "public_error.html"

        # Private URLs must not reveal whether a tenant exists.
        if status_code in (401, 403, 404):
            heading = "Link unavailable"
            message = (
                "This link may have expired or been revoked."
            )

    else:
        template_name = "app_error.html"

        if is_owner_page(request.url.path):
            if status_code in (
                400,
                403,
                409,
                413,
                415,
                422,
            ):
                feedback = owner_feedback(
                    status_code,
                    detail,
                )

                message = feedback.message
                billing_link = feedback.billing_link

                if billing_link:
                    heading = "Subscription action required"

    return templates.TemplateResponse(
        request,
        template_name,
        {
            "request": request,
            "settings": settings,
            "heading": heading,
            "message": message,
            "billing_link": billing_link,
        },
        status_code=status_code,
        headers=no_store_headers(),
    )


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
):
    if wants_html(request):
        response = page_error(
            request,
            exc.status_code,
            exc.detail,
        )

        if exc.headers:
            for key, value in exc.headers.items():
                if key.lower() not in {
                    "cache-control",
                    "x-robots-tag",
                }:
                    response.headers[key] = value

        return response

    response = JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
        },
        headers=no_store_headers(),
    )

    if exc.headers:
        for key, value in exc.headers.items():
            if key.lower() not in {
                "cache-control",
                "x-robots-tag",
            }:
                response.headers[key] = value

    return response


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
):
    if wants_html(request):
        return page_error(
            request,
            422,
        )

    return JSONResponse(
        status_code=422,
        content={
            "detail": "Request validation failed.",
        },
        headers=no_store_headers(),
    )


async def unexpected_exception_handler(
    request: Request,
    exc: Exception,
):
    # Do not log bearer-token path parameters.
    LOGGER.exception(
        "Unhandled application request on %s %s",
        request.method,
        request.url.path.split("/", 2)[:2],
        exc_info=exc,
    )

    if wants_html(request):
        return page_error(
            request,
            500,
        )

    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal service error.",
        },
        headers=no_store_headers(),
    )
