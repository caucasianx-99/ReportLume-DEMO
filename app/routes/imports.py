"""Preview and confirm X Content CSV imports."""

import hashlib
import hmac
import os
import secrets
import time

from urllib.parse import quote, unquote

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)

from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.release_policy import customer_data_ready
from app.core.csrf import verify_csrf

from app.core.firestore_rest import (
    FirestoreNotFound,
    FirestoreUnavailable,
)

from app.routes.clients import (
    render_page,
    require_client_id,
    workspace_for,
)

from app.services.client_workspace import (
    WorkspaceReadOnly,
)

from app.services.csv_imports import (
    CsvImportService,
    ImportConflict,
    ImportValidationError,
    PlanSelectionRequired,
    import_context,
)

from app.services.x_content_csv import (
    MAX_BYTES,
    CsvValidationError,
    parse_content_csv,
    parse_duplicate_choices,
)


router = APIRouter(tags=["imports"])

_DEV_SIGNING_KEY = secrets.token_bytes(32)


def _signing_key() -> bytes:
    configured = os.getenv(
        "IMPORT_PREVIEW_SECRET", ""
    )

    if len(configured) >= 32:
        return configured.encode("utf-8")

    if settings.production:
        raise HTTPException(
            503,
            "Import preview signing is not configured.",
        )

    return _DEV_SIGNING_KEY


def _preview_receipt(
    uid: str,
    client_id: str,
    checksum: str,
) -> str:
    expires = int(time.time()) + 900

    message = (
        f"{uid}:{client_id}:{checksum}:{expires}"
    )

    signature = hmac.new(
        _signing_key(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()

    return f"{expires}.{signature}"


def _check_receipt(
    token: str,
    uid: str,
    client_id: str,
    checksum: str,
):
    if len(token) > 100:
        raise HTTPException(
            403,
            "Preview expired; preview the CSV again.",
        )

    parts = token.split(".", 1)

    if (
        len(parts) != 2
        or not parts[0].isdigit()
    ):
        raise HTTPException(
            403,
            "Preview required before confirmation.",
        )

    expires = int(parts[0])

    if (
        expires < int(time.time())
        or expires > int(time.time()) + 900
    ):
        raise HTTPException(
            403,
            "Preview expired; preview the CSV again.",
        )

    message = (
        f"{uid}:{client_id}:{checksum}:{expires}"
    )

    expected = hmac.new(
        _signing_key(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        expected,
        parts[1],
    ):
        raise HTTPException(
            403,
            "Invalid preview receipt.",
        )


async def _csv_bytes(request: Request) -> bytes:
    content_type = request.headers.get(
        "content-type", ""
    ).split(";", 1)[0]

    if content_type != "text/csv":
        raise HTTPException(
            415,
            "CSV content type required.",
        )

    content = bytearray()

    async for chunk in request.stream():
        content.extend(chunk)

        if len(content) > MAX_BYTES:
            raise HTTPException(
                413,
                "CSV exceeds the 10 MiB limit.",
            )

    if not content:
        raise HTTPException(
            400,
            "CSV is empty.",
        )

    return bytes(content)


async def _context(
    request: Request,
    client_id: str,
):
    require_client_id(client_id)

    # A production deployment may be reachable before legal/security QA.
    # Never parse or accept a real client CSV during that interval.
    if not customer_data_ready(settings):
        raise HTTPException(
            503,
            "Client CSV imports are not enabled for this deployment.",
        )

    workspace = await workspace_for(request)

    if workspace is None:
        raise HTTPException(
            401,
            "Sign in to continue.",
        )

    try:
        service = CsvImportService(
            workspace.store
        )

        await run_in_threadpool(
            service._check_access,
            client_id,
        )

        return service

    except FirestoreNotFound:
        raise HTTPException(
            404,
            "Client not found.",
        )

    except WorkspaceReadOnly as exc:
        raise HTTPException(
            403,
            str(exc),
        ) from exc

    except ImportValidationError as exc:
        raise HTTPException(
            403,
            str(exc),
        ) from exc

    except FirestoreUnavailable:
        raise HTTPException(
            503,
            "Workspace is temporarily unavailable.",
        )


@router.get(
    "/clients/{client_id}/import",
    response_class=HTMLResponse,
)
async def import_page(
    request: Request,
    client_id: str,
):
    require_client_id(client_id)

    workspace = await workspace_for(request)

    if workspace is None:
        return RedirectResponse(
            "/login",
            status_code=303,
        )

    try:
        service = CsvImportService(
            workspace.store
        )

        client = await run_in_threadpool(
            service.store.get_client,
            client_id,
        )

        uploads = await run_in_threadpool(
            service.list_uploads,
            client_id,
        )

        access_error = ""

        try:
            await run_in_threadpool(
                service._check_access,
                client_id,
            )

        except (
            WorkspaceReadOnly,
            ImportValidationError,
        ) as exc:
            access_error = str(exc)

        if not customer_data_ready(settings):
            access_error = (
                "Real client CSV imports are disabled until production "
                "privacy and security review is complete."
            )

    except FirestoreNotFound:
        raise HTTPException(
            404,
            "Client not found.",
        )

    except FirestoreUnavailable:
        raise HTTPException(
            503,
            "Workspace temporarily unavailable.",
        )

    return render_page(
        request,
        "import_csv.html",
        {
            "client": client,
            "uploads": uploads,
            "access_error": access_error,
        },
    )


@router.post(
    "/api/clients/{client_id}/imports/preview"
)
async def preview_csv(
    request: Request,
    client_id: str,
):
    verify_csrf(
        request,
        request.headers.get("x-csrf-token", ""),
    )

    service = await _context(
        request,
        client_id,
    )

    raw = await _csv_bytes(request)

    filename = unquote(
        request.headers.get(
            "x-upload-filename", ""
        )
    )[:255]

    try:
        parsed = parse_content_csv(
            raw,
            filename,
        )

    except CsvValidationError as exc:
        raise HTTPException(
            400,
            str(exc),
        ) from exc

    # Every row a choice could keep, so date changes are shown up front.
    candidates = parsed["rows"] + [
        {"post_id": group["post_id"], "published_date": row["date"]}
        for group in parsed["duplicate_conflicts"]
        for row in group["rows"]
    ]

    try:
        overlap = await run_in_threadpool(
            service.overlap,
            client_id,
            candidates,
        )

    except FirestoreUnavailable as exc:
        raise HTTPException(
            503,
            "Workspace is temporarily unavailable.",
        ) from exc

    receipt = _preview_receipt(
        service.store.uid,
        client_id,
        parsed["file_sha256"],
    )

    return JSONResponse(
        {
            "preview_token": receipt,
            "row_count": parsed["row_count"],
            "columns": parsed["columns"],
            "missing_columns": (
                parsed["missing_columns"]
            ),
            "warnings": parsed["warnings"],
            "published_start": (
                parsed["published_start"]
            ),
            "published_end": (
                parsed["published_end"]
            ),
            "months": parsed["months"],
            "possible_replies": (
                parsed["possible_replies"]
            ),
            "zero_impressions": (
                parsed["zero_impressions"]
            ),
            "engagement_mismatches": (
                parsed["engagement_mismatches"]
            ),
            "suggested_export": (
                parsed["suggested_export"]
            ),
            "duplicates_merged": parsed["duplicates_merged"],
            "duplicate_conflicts": parsed["duplicate_conflicts"],
            "repaired_post_ids": parsed["repaired_post_ids"],
            "already_imported": overlap["already_imported"],
            "date_change_count": overlap["date_change_count"],
            "date_changes": overlap["date_changes"],
            "content_change_count": overlap["content_change_count"],
            "sample": [
                {
                    "post_id": row["post_id"],
                    "date": row["published_date"],
                    "text": row["post_text"][:120],
                }
                for row in parsed["rows"][:3]
            ],
        },
        headers={
            "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex",
        },
    )


@router.post(
    "/api/clients/{client_id}/imports/confirm"
)
async def confirm_csv(
    request: Request,
    client_id: str,
):
    verify_csrf(
        request,
        request.headers.get("x-csrf-token", ""),
    )

    service = await _context(
        request,
        client_id,
    )

    raw = await _csv_bytes(request)

    try:
        parsed = parse_content_csv(
            raw,
            duplicate_choices=parse_duplicate_choices(
                request.headers.get("x-duplicate-choices", "")
            ),
        )

        _check_receipt(
            request.headers.get(
                "x-preview-token", ""
            ),
            service.store.uid,
            client_id,
            parsed["file_sha256"],
        )

        if parsed["needs_duplicate_choice"]:
            raise HTTPException(
                400,
                "Choose which row to keep for each post that appears "
                "more than once.",
            )

        if (
            request.headers.get(
                "x-confirm-import"
            ) != "yes"
        ):
            raise HTTPException(
                400,
                "Explicit import confirmation is required.",
            )

        context = import_context(
            request.headers,
            parsed,
        )

        overlap = await run_in_threadpool(
            service.overlap,
            client_id,
            parsed["rows"],
        )

        if (
            overlap["date_change_count"]
            and request.headers.get("x-accept-date-changes") != "yes"
        ):
            raise HTTPException(
                409,
                f"{overlap['date_change_count']} posts were imported "
                "before with a different publication date. Review them "
                "in the preview and confirm the change.",
            )

        outcome = await run_in_threadpool(
            service.confirm,
            client_id,
            parsed,
            context,
        )

        return JSONResponse(
            outcome,
            headers={
                "Cache-Control": "no-store",
            },
        )

    except (
        CsvValidationError,
        ImportValidationError,
    ) as exc:
        raise HTTPException(
            400,
            str(exc),
        ) from exc

    except ImportConflict as exc:
        raise HTTPException(
            409,
            str(exc),
        ) from exc

    except WorkspaceReadOnly as exc:
        raise HTTPException(
            403,
            str(exc),
        ) from exc

    except FirestoreNotFound:
        raise HTTPException(
            404,
            "Client not found.",
        )

    except FirestoreUnavailable:
        raise HTTPException(
            503,
            "Import storage temporarily unavailable.",
        )
