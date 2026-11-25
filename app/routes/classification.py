
"""Authenticated post classification routes."""

import json

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

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
    has_paid_access,
    require_write_access,
)
from app.services.proof_of_work import (
    month_bounds,
    ProofError,
)
from app.services.monthly_defaults import (
    previous_completed_month,
)
from app.services.post_classification import (
    ClassificationConflict,
    ClassificationError,
    PostClassificationService,
)


router = APIRouter(tags=["post-classification"])


async def read_small_json(request: Request):
    content_type = request.headers.get(
        "content-type",
        "",
    ).split(";", 1)[0].strip().lower()

    if content_type != "application/json":
        raise HTTPException(
            415,
            "JSON content type required.",
        )

    body = bytearray()

    async for chunk in request.stream():
        body.extend(chunk)

        if len(body) > 32768:
            raise HTTPException(
                413,
                "Classification request too large.",
            )

    try:
        result = json.loads(
            body.decode("utf-8")
        )

    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(
            400,
            "Invalid JSON.",
        ) from exc

    if not isinstance(result, dict):
        raise HTTPException(
            400,
            "JSON object required.",
        )

    return result


@router.get("/clients/{client_id}/posts")
async def classification_page(
    request: Request,
    client_id: str,
    page_token: str = "",
    period: str = "",
    history: str = "",
):
    require_client_id(client_id)

    workspace = await workspace_for(request)

    if workspace is None:
        return RedirectResponse(
            "/start",
            status_code=303,
        )

    if not period:
        period = previous_completed_month()

    try:
        first_day, _ = month_bounds(period)

    except (ProofError, TypeError, ValueError) as exc:
        raise HTTPException(
            400,
            "Invalid reporting month.",
        ) from exc

    previous_period = (
        first_day - timedelta(days=1)
    ).strftime("%Y-%m")

    if len(page_token) > 256:
        raise HTTPException(
            400,
            "Invalid page token.",
        )

    if len(history) > 4500:
        raise HTTPException(
            400,
            "Invalid page history.",
        )

    entries = history.split(".") if history else []

    if len(entries) > 25:
        raise HTTPException(
            400,
            "Invalid page history.",
        )

    start, end = month_bounds(period)

    for entry in entries:
        if entry != "~":
            try:
                PostClassificationService._decode_cursor(
                    entry,
                    start.isoformat(),
                    end.isoformat(),
                )

            except ClassificationError as exc:
                raise HTTPException(
                    400,
                    str(exc),
                ) from exc

    previous_page_token = (
        "" if entries[-1] == "~" else entries[-1]
    ) if entries else None

    previous_history = ".".join(
        entries[:-1]
    )

    next_history = ".".join(
        entries + (
            [page_token]
            if page_token
            else ["~"]
        )
    )

    service = PostClassificationService(
        workspace.store
    )

    try:
        client = await run_in_threadpool(
            workspace.get_client,
            client_id,
        )

        result = await run_in_threadpool(
            service.list_posts,
            client_id,
            page_token,
            period,
        )

        profile, _ = await run_in_threadpool(
            workspace.store.get_user_with_version
        )

    except ClassificationError as exc:
        raise HTTPException(
            400,
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
            "Post records temporarily unavailable.",
        )

    locked = False

    try:
        require_write_access(profile)

    except WorkspaceReadOnly:
        locked = True

    editable = (
        client.get("status") == "active"
        and not locked
        and (
            has_paid_access(profile)
            or profile.get("unpaid_client_id") == client_id
        )
    )

    return render_page(
        request,
        "classification.html",
        {
            "client": client,
            "posts": result["posts"],
            "next_page_token": result[
                "next_page_token"
            ],
            "excluded_processing": result[
                "excluded_processing"
            ],
            "editable": editable,
            "period": period,
            "previous_period": previous_period,
            "previous_page_token": previous_page_token,
            "previous_history": previous_history,
            "next_history": next_history,
        },
    )


@router.post(
    "/api/clients/{client_id}/posts/classify"
)
async def classify_posts(
    request: Request,
    client_id: str,
):
    require_client_id(client_id)

    verify_csrf(
        request,
        request.headers.get(
            "x-csrf-token",
            "",
        ),
    )

    workspace = await workspace_for(request)

    if workspace is None:
        raise HTTPException(
            401,
            "Authentication required.",
        )

    data = await read_small_json(request)

    service = PostClassificationService(
        workspace.store
    )

    try:
        updated = await run_in_threadpool(
            service.apply_patches,
            client_id,
            data.get("patches"),
        )

    except ClassificationError as exc:
        raise HTTPException(
            400,
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

    except ClassificationConflict as exc:
        raise HTTPException(
            409,
            str(exc),
        ) from exc

    except FirestoreUnavailable:
        raise HTTPException(
            503,
            "Classification storage unavailable.",
        )

    return JSONResponse(
        {
            "updated": updated,
            "count": len(updated),
        },
        headers={
            "Cache-Control": "no-store",
        },
    )
