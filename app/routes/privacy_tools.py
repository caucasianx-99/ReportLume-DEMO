"""Private client-data exports and explicitly gated deletion actions."""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)

from starlette.concurrency import run_in_threadpool

from app.core.config import settings

from app.core.firestore_rest import (
    FirestoreConflict,
    FirestoreNotFound,
    FirestoreUnavailable,
)

from app.routes.auth import (
    current_user,
    read_form,
)

from app.routes.clients import (
    render_page,
    require_client_id,
    workspace_for,
)

from app.services.data_lifecycle import (
    DataDeletionDisabled,
    DataLimit,
    delete_client,
    export_client_chunks,
)


router = APIRouter(
    tags=["privacy-tools"]
)


@router.get(
    "/settings/data",
    response_class=HTMLResponse,
)
async def data_settings(request: Request):
    workspace = await workspace_for(request)

    if workspace is None:
        return RedirectResponse(
            "/start",
            status_code=303,
        )

    page_token = request.query_params.get("page", "")

    try:
        clients, next_token = await run_in_threadpool(
            workspace.store.list_clients,
            page_token,
        )

    except (FirestoreUnavailable, ValueError) as exc:
        raise HTTPException(
            503,
            "Data listing is temporarily unavailable.",
        ) from exc

    return render_page(
        request,
        "data_settings.html",
        {
            "clients": clients,
            "next_token": next_token,
            "deletion_enabled": (
                settings.data_deletion_enabled
            ),
        },
    )


@router.get("/clients/{client_id}/export.json")
async def export_client(
    request: Request,
    client_id: str,
):
    require_client_id(client_id)

    workspace = await workspace_for(request)

    if workspace is None:
        raise HTTPException(
            401,
            "Workspace required",
        )

    try:
        # The client record is read here, so a missing client is a 404.
        # The rest streams; an interrupted download lacks "complete": true.
        chunks = await run_in_threadpool(
            export_client_chunks,
            workspace.store,
            client_id,
        )

    except FirestoreNotFound as exc:
        raise HTTPException(
            404,
            "Client not found",
        ) from exc

    except DataLimit as exc:
        raise HTTPException(
            413,
            "Export exceeds the safe interactive size.",
        ) from exc

    except FirestoreUnavailable as exc:
        raise HTTPException(
            503,
            "Data export is temporarily unavailable.",
        ) from exc

    return StreamingResponse(
        (chunk.encode("utf-8") for chunk in chunks),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="reportlume-client-{client_id}.json"'
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/clients/{client_id}/delete")
async def remove_client(
    request: Request,
    client_id: str,
):
    require_client_id(client_id)

    workspace = await workspace_for(request)

    if workspace is None:
        raise HTTPException(
            401,
            "Workspace required",
        )

    # read_form verifies the existing CSRF protection.
    data = await read_form(request)

    if not settings.data_deletion_enabled:
        raise HTTPException(
            503,
            "Self-service deletion is not enabled.",
        )

    try:
        client = await run_in_threadpool(
            workspace.store.get_client,
            client_id,
        )

        expected_name = client.get(
            "display_name"
        )

        submitted_name = data.get(
            "confirm_name",
            "",
        )

        if (
            not isinstance(expected_name, str)
            or submitted_name != expected_name
        ):
            raise HTTPException(
                400,
                "Enter the exact client name to confirm deletion.",
            )

        await run_in_threadpool(
            delete_client,
            workspace.store,
            client_id,
        )

    except FirestoreNotFound as exc:
        raise HTTPException(
            404,
            "Client not found",
        ) from exc

    except DataLimit as exc:
        raise HTTPException(
            413,
            "Deletion exceeds the safe interactive size.",
        ) from exc

    except DataDeletionDisabled as exc:
        raise HTTPException(
            503,
            "Deletion is disabled.",
        ) from exc

    except FirestoreConflict as exc:
        raise HTTPException(
            409,
            "Client data changed. Review the record before retrying.",
        ) from exc

    except FirestoreUnavailable as exc:
        raise HTTPException(
            503,
            "Deletion did not complete. "
            "Do not assume the data was erased.",
        ) from exc

    return RedirectResponse(
        "/clients",
        status_code=303,
    )
