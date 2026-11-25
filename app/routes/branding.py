"""Authenticated and CSRF-protected agency branding settings."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.core.csrf import verify_csrf
from app.core.firestore_rest import FirestoreUnavailable

from app.routes.clients import render_page, workspace_for

from app.services.branding import BrandValidation, save_brand
from app.services.workspace_access import plan_entitlements
from app.core.config import settings
from app.services.client_workspace import (
    WorkspaceReadOnly,
    require_write_access,
)


router = APIRouter(tags=["branding"])


def _can_edit(profile):
    try:
        require_write_access(profile)
        return True
    except WorkspaceReadOnly:
        return False


def _render_settings(
    request,
    profile,
    *,
    message="",
    error="",
    status_code=200,
):
    editable = _can_edit(profile)
    rights = plan_entitlements(profile, settings)

    return render_page(
        request,
        "brand_settings.html",
        {
            "profile": profile,
            "message": message,
            "error": error,
            "editable": editable,
            "custom_branding": (
                editable and rights.get(
                    "allow_custom_branding",
                    False,
                )
            ),
        },
        status_code=status_code,
    )


@router.get(
    "/settings/brand",
    response_class=HTMLResponse,
)
async def brand_page(request: Request, saved: int = 0):
    workspace = await workspace_for(request)

    if workspace is None:
        return RedirectResponse(
            "/start",
            status_code=303,
        )

    try:
        profile, _ = await run_in_threadpool(
            workspace.store.get_user_with_version
        )
    except FirestoreUnavailable as exc:
        raise HTTPException(
            503,
            "Settings are temporarily unavailable.",
        ) from exc

    return _render_settings(
        request,
        profile,
        message=(
            "Agency branding saved. New report versions will "
            "use these settings."
            if saved == 1
            else ""
        ),
    )


@router.post(
    "/settings/brand",
    response_class=HTMLResponse,
)
async def update_brand(request: Request):
    workspace = await workspace_for(request)

    if workspace is None:
        raise HTTPException(
            401,
            "Workspace required",
        )

    content_type = request.headers.get(
        "content-type",
        "",
    ).lower()

    if not content_type.startswith("multipart/form-data"):
        raise HTTPException(
            415,
            "Branding settings require a multipart form.",
        )

    raw_length = request.headers.get(
        "content-length",
        "",
    )

    if (
        not raw_length.isdecimal()
        or int(raw_length) > 250_000
    ):
        raise HTTPException(
            413,
            "Branding upload must be at most 250 KB.",
        )

    async with request.form(
        max_files=1,
        max_fields=5,
    ) as form:
        verify_csrf(
            request,
            str(form.get("csrf_token", "")),
        )

        raw_brand_name = form.get("brand_name", "")

        if not isinstance(raw_brand_name, str):
            raise HTTPException(
                400,
                "Invalid agency name field.",
            )

        brand_name = raw_brand_name

        requested_theme = form.get("brand_theme")

        if (
            requested_theme is not None
            and not isinstance(requested_theme, str)
        ):
            raise HTTPException(
                400,
                "Invalid brand color field.",
            )

        brand_theme = (
            str(requested_theme)
            if requested_theme is not None
            else None
        )

        remove_logo = (
            form.get("remove_logo", "") == "on"
        )

        raw_logo = form.get("logo")

        if (
            raw_logo is not None
            and not isinstance(raw_logo, UploadFile)
        ):
            raise HTTPException(
                400,
                "Invalid logo upload field.",
            )

        logo = None

        if (
            isinstance(raw_logo, UploadFile)
            and raw_logo.filename
        ):
            logo = await raw_logo.read(200_001)

        try:
            await run_in_threadpool(
                save_brand,
                workspace.store,
                brand_name,
                brand_theme,
                logo,
                remove_logo,
            )

        except (BrandValidation, WorkspaceReadOnly) as exc:
            status_code = (
                400
                if isinstance(exc, BrandValidation)
                else 403
            )

            try:
                profile, _ = await run_in_threadpool(
                    workspace.store.get_user_with_version
                )
            except FirestoreUnavailable as storage_exc:
                raise HTTPException(
                    503,
                    "Branding settings are temporarily unavailable.",
                ) from storage_exc

            retained_profile = {
                **profile,
                "brand_name": brand_name,
                "brand_theme": (
                    brand_theme
                    if brand_theme in {"teal", "blue", "slate"}
                    else profile.get("brand_theme", "teal")
                ),
            }

            return _render_settings(
                request,
                retained_profile,
                error=str(exc),
                status_code=status_code,
            )

        except FirestoreUnavailable as exc:
            raise HTTPException(
                503,
                "Branding update is temporarily unavailable.",
            ) from exc

    return RedirectResponse(
        "/settings/brand?saved=1",
        status_code=303,
    )
