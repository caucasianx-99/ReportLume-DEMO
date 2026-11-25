"""Authenticated client workspace routes."""

import json
import uuid

from urllib.parse import quote

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
)
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.release_policy import account_creation_ready
from app.core.csrf import (
    attach_csrf_cookie,
    csrf_token_for,
)
from app.core.firestore_rest import (
    FirestoreNotFound,
    FirestoreUnavailable,
    TenantStore,
)
from app.routes.auth import (
    current_user,
    read_form,
)
from app.routes.public import templates
from app.services.client_workspace import (
    CLIENT_ID_PATTERN,
    ClientWorkspace,
    ClientValidationError,
    ClientQuotaError,
    WorkspaceBusy,
    WorkspaceReadOnly,
    parse_client_form,
    require_write_access,
    has_paid_access,
    client_limit_for,
    selection_required,
)


router = APIRouter(
    prefix="/clients",
    tags=["clients"],
)


async def workspace_for(request: Request):
    try:
        claims = await current_user(request)
    except Exception:
        raise HTTPException(
            503,
            "Authentication temporarily unavailable",
        )

    if claims is None:
        return None

    try:
        return ClientWorkspace(
            TenantStore(claims["uid"])
        )
    except (
        KeyError,
        ValueError,
        FirestoreUnavailable,
    ):
        raise HTTPException(
            503,
            "Workspace temporarily unavailable",
        )


def require_client_id(client_id: str):
    if not CLIENT_ID_PATTERN.fullmatch(client_id):
        raise HTTPException(
            404,
            "Client not found",
        )


def render_page(
    request: Request,
    template_name: str,
    values: dict,
    status_code: int = 200,
):
    token = csrf_token_for(request)

    response = templates.TemplateResponse(
        request,
        template_name,
        {
            "request": request,
            "settings": settings,
            "csrf_token": token,
            **values,
        },
        status_code=status_code,
    )

    attach_csrf_cookie(
        response,
        token,
    )

    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex"

    return response


def _all_client_choices(store):
    """Obtain the complete owner directory for a capacity change."""
    rows = []
    token = ""
    seen = set()

    for _ in range(500):
        page, following = (
            store.list_clients(token)
        )

        if (
            not isinstance(page, list)
            or not isinstance(following, str)
        ):
            raise WorkspaceBusy(
                "Client list is incomplete"
            )

        rows.extend(page)

        if len(rows) > 10000:
            raise WorkspaceBusy(
                "Client list exceeds safety limit"
            )

        if not following:
            return [
                row
                for row in rows
                if row.get("status")
                in {"active", "paused"}
            ]

        if (
            len(following) > 2048
            or following in seen
            or following == token
        ):
            raise WorkspaceBusy(
                "Invalid pagination"
            )

        seen.add(token)
        token = following

    raise WorkspaceBusy(
        "Client list is incomplete"
    )


def unavailable():
    raise HTTPException(
        status_code=503,
        detail=(
            "Workspace data is temporarily unavailable."
        ),
    )


def map_write_error(exc):
    if isinstance(exc, ClientValidationError):
        return 400, str(exc)

    if isinstance(exc, ClientQuotaError):
        return 403, str(exc)

    if isinstance(exc, WorkspaceReadOnly):
        return 403, str(exc)

    if isinstance(exc, WorkspaceBusy):
        return 409, str(exc)

    return (
        503,
        "The operation could not be completed.",
    )


@router.get(
    "",
    response_class=HTMLResponse,
)
async def client_list(
    request: Request,
    page_token: str = "",
    *,
    selection_error: str = "",
    status_code: int = 200,
):
    workspace = await workspace_for(request)

    if workspace is None:
        return RedirectResponse(
            "/login",
            status_code=303,
        )

    if len(page_token) > 2048:
        raise HTTPException(
            400,
            "Invalid page token",
        )

    try:
        clients, next_token = (
            await run_in_threadpool(
                workspace.list_clients,
                page_token,
            )
        )

        profile, _ = await run_in_threadpool(
            workspace.store.get_user_with_version
        )

    except (
        FirestoreUnavailable,
        ValueError,
    ):
        unavailable()

    paid = has_paid_access(profile)

    try:
        require_write_access(profile)
        locked = False
    except WorkspaceReadOnly:
        locked = True

    # Pre-trial accounts may have one client without paid access.
    can_create = (
        not locked
        and (
            int(
                profile.get(
                    "active_client_count",
                    0,
                )
            )
            < client_limit_for(profile)
        )
        and (
            paid
            or not profile.get(
                "unpaid_client_created",
                False,
            )
        )
    )

    capacity_limit = client_limit_for(profile)
    choices = []
    selected_ids = []

    if selection_required(profile):
        try:
            choices = await run_in_threadpool(
                _all_client_choices,
                workspace.store,
            )

            pinned = profile.get(
                "capacity_selection_kept_ids_json"
            )

            if pinned:
                selected_ids = json.loads(pinned)

        except (
            FirestoreUnavailable,
            WorkspaceBusy,
            ValueError,
            TypeError,
        ):
            unavailable()

    next_url = (
        "/clients?page_token="
        + quote(next_token, safe="")
        if next_token
        else ""
    )

    return render_page(
        request,
        "clients.html",
        {
            "clients": clients,
            "next_url": next_url,
            "profile": profile,
            "paid": paid,
            "locked": locked,
            "can_create": can_create,
            "active_count": int(
                profile.get(
                    "active_client_count",
                    0,
                )
            ),
            "client_limit": client_limit_for(profile),
            "selection_required": selection_required(profile),
            "selection_error": selection_error,
            "selection_saved": (
                request.query_params.get(
                    "selection_saved"
                ) == "1"
            ),
            "solo_client_ids": [
                client["id"]
                for client in clients
                if client.get("status")
                in {"active", "paused"}
            ],
            "capacity_limit": capacity_limit,
            "capacity_candidates": choices,
            "capacity_pinned_ids": selected_ids,
            "capacity_selection_started": bool(
                profile.get(
                    "capacity_selection_kept_ids_json"
                )
            ),
        },
        status_code=status_code,
    )


@router.post(
    "/keep-active",
    response_class=HTMLResponse,
)
async def keep_solo_clients(request: Request):
    workspace = await workspace_for(request)

    if workspace is None:
        raise HTTPException(
            401,
            "Authentication required",
        )

    # Includes mandatory double-submit CSRF.
    data = await read_form(request)

    resume = data.get("resume") == "1"
    selected = []

    for key, value in data.items():
        if not key.startswith("keep_"):
            continue

        identifier = key[5:]

        if (
            not CLIENT_ID_PATTERN.fullmatch(identifier)
            or value != "on"
        ):
            raise HTTPException(
                400,
                "Invalid client choice",
            )

        selected.append(identifier)

    try:
        complete = await run_in_threadpool(
            workspace.select_capacity_clients,
            None if resume else selected,
        )

    except (
        ClientValidationError,
        ClientQuotaError,
        WorkspaceReadOnly,
        WorkspaceBusy,
    ) as exc:
        status, message = map_write_error(exc)

        return await client_list(
            request,
            selection_error=message,
            status_code=status,
        )

    except FirestoreUnavailable:
        unavailable()

    return RedirectResponse(
        (
            "/clients?selection_saved=1"
            if complete
            else "/clients?selection_progress=1"
        ),
        status_code=303,
    )


def require_account_creation():
    """Production before its approvals stores no new client records."""
    if not account_creation_ready(settings):
        raise HTTPException(
            503,
            "New clients can be added once Reportlume opens for customers.",
        )


@router.get(
    "/new",
    response_class=HTMLResponse,
)
async def new_client_page(request: Request):
    workspace = await workspace_for(request)

    if workspace is None:
        return RedirectResponse(
            "/login",
            status_code=303,
        )

    require_account_creation()

    return render_page(
        request,
        "client_form.html",
        {
            "mode": "create",
            "client": {},
            "error": "",
            "form_id": uuid.uuid4().hex,
        },
    )


@router.post(
    "",
    response_class=HTMLResponse,
)
async def create_client(request: Request):
    workspace = await workspace_for(request)

    if workspace is None:
        raise HTTPException(
            401,
            "Authentication required",
        )

    data = await read_form(request)
    require_account_creation()
    form_id = data.get(
        "client_id",
        "",
    )

    try:
        client_id = await run_in_threadpool(
            workspace.create_client,
            data,
            form_id,
        )

    except (
        ClientValidationError,
        ClientQuotaError,
        WorkspaceReadOnly,
        WorkspaceBusy,
    ) as exc:
        status, message = map_write_error(exc)

        return render_page(
            request,
            "client_form.html",
            {
                "mode": "create",
                "client": data,
                "error": message,
                "form_id": (
                    form_id
                    or uuid.uuid4().hex
                ),
            },
            status_code=status,
        )

    except FirestoreUnavailable:
        unavailable()

    return RedirectResponse(
        f"/clients/{client_id}",
        status_code=303,
    )


@router.get(
    "/{client_id}",
    response_class=HTMLResponse,
)
async def client_detail(
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
        client = await run_in_threadpool(
            workspace.get_client,
            client_id,
        )

        reports, remaining = await run_in_threadpool(
            workspace.list_reports,
            client_id,
        )

    except FirestoreNotFound:
        raise HTTPException(
            404,
            "Client not found",
        )
    except FirestoreUnavailable:
        unavailable()

    return render_page(
        request,
        "client_detail.html",
        {
            "client": client,
            "reports": reports,
            "has_more_reports": bool(remaining),
        },
    )


@router.get(
    "/{client_id}/edit",
    response_class=HTMLResponse,
)
async def edit_client_page(
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
        client = await run_in_threadpool(
            workspace.get_client,
            client_id,
        )
    except FirestoreNotFound:
        raise HTTPException(
            404,
            "Client not found",
        )
    except FirestoreUnavailable:
        unavailable()

    if client.get("status") == "archived":
        raise HTTPException(
            403,
            "Archived clients are read-only",
        )

    return render_page(
        request,
        "client_form.html",
        {
            "mode": "edit",
            "client": client,
            "error": "",
            "form_id": "",
        },
    )


@router.post(
    "/{client_id}/edit",
    response_class=HTMLResponse,
)
async def edit_client(
    request: Request,
    client_id: str,
):
    require_client_id(client_id)

    workspace = await workspace_for(request)

    if workspace is None:
        raise HTTPException(
            401,
            "Authentication required",
        )

    data = await read_form(request)

    try:
        await run_in_threadpool(
            workspace.update_client,
            client_id,
            data,
        )

    except FirestoreNotFound:
        raise HTTPException(
            404,
            "Client not found",
        )

    except (
        ClientValidationError,
        ClientQuotaError,
        WorkspaceReadOnly,
        WorkspaceBusy,
    ) as exc:
        status, message = map_write_error(exc)

        return render_page(
            request,
            "client_form.html",
            {
                "mode": "edit",
                "client": {
                    **data,
                    "id": client_id,
                },
                "error": message,
                "form_id": "",
            },
            status_code=status,
        )

    except FirestoreUnavailable:
        unavailable()

    return RedirectResponse(
        f"/clients/{client_id}",
        status_code=303,
    )


@router.post("/{client_id}/status")
async def change_client_status(
    request: Request,
    client_id: str,
):
    require_client_id(client_id)

    workspace = await workspace_for(request)

    if workspace is None:
        raise HTTPException(
            401,
            "Authentication required",
        )

    data = await read_form(request)
    new_status = data.get(
        "status",
        "",
    )

    try:
        await run_in_threadpool(
            workspace.change_status,
            client_id,
            new_status,
        )

    except FirestoreNotFound:
        raise HTTPException(
            404,
            "Client not found",
        )

    except (
        ClientValidationError,
        ClientQuotaError,
        WorkspaceReadOnly,
        WorkspaceBusy,
    ) as exc:
        status, message = map_write_error(exc)
        raise HTTPException(
            status,
            message,
        )

    except FirestoreUnavailable:
        unavailable()

    return RedirectResponse(
        f"/clients/{client_id}",
        status_code=303,
    )
