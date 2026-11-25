
"""Owner-protected Proof of Work pages and form actions."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.core.firestore_rest import (
    FirestoreNotFound,
    FirestoreUnavailable,
)

from app.routes.auth import read_form

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

from app.services.monthly_defaults import (
    previous_completed_month,
)

from app.services.proof_of_work import (
    ProofConflict,
    ProofError,
    ProofOfWorkService,
    month_bounds,
)


router = APIRouter(tags=["proof-of-work"])


def checked_period(value: str) -> str:
    period = value or previous_completed_month()

    try:
        month_bounds(period)

    except ProofError as exc:
        raise HTTPException(
            400,
            str(exc),
        ) from exc

    return period


@router.get(
    "/clients/{client_id}/proof",
    response_class=HTMLResponse,
)
async def proof_page(
    request: Request,
    client_id: str,
    period: str = "",
    saved: int = 0,
):
    require_client_id(client_id)
    period = checked_period(period)

    workspace = await workspace_for(request)

    if workspace is None:
        return RedirectResponse(
            "/start",
            status_code=303,
        )

    service = ProofOfWorkService(
        workspace.store
    )

    try:
        proof, version, client = (
            await run_in_threadpool(
                service.overview,
                client_id,
                period,
            )
        )

        profile, _ = await run_in_threadpool(
            workspace.store.get_user_with_version
        )

    except FirestoreNotFound:
        raise HTTPException(
            404,
            "Client not found.",
        )

    except FirestoreUnavailable:
        raise HTTPException(
            503,
            "Proof of Work data temporarily unavailable.",
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
        "proof_of_work.html",
        {
            "client": client,
            "proof": proof,
            "manual": proof["manual"],
            "version": version,
            "editable": editable,
            "saved": saved == 1,
        },
    )


@router.post(
    "/clients/{client_id}/proof",
    response_class=HTMLResponse,
)
async def save_proof(
    request: Request,
    client_id: str,
):
    require_client_id(client_id)

    workspace = await workspace_for(request)

    if workspace is None:
        raise HTTPException(
            401,
            "Authentication required.",
        )

    data = await read_form(request)

    period = checked_period(
        data.get("period", "")
    )

    service = ProofOfWorkService(
        workspace.store
    )

    try:
        await run_in_threadpool(
            service.save_manual,
            client_id,
            period,
            data,
            data.get("expected_version", ""),
        )

    except ProofError as exc:
        raise HTTPException(
            400,
            str(exc),
        ) from exc

    except ProofConflict as exc:
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
            "Could not save delivery information.",
        )

    return RedirectResponse(
        f"/clients/{client_id}/proof?period={period}&saved=1",
        status_code=303,
    )
