"""Owner-only, no-write previews of draft client reports."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.core.firestore_rest import (
    FirestoreConflict,
    FirestoreNotFound,
    FirestoreUnavailable,
)

from app.routes.reports import (
    read_report_form,
    _page_data,
    render_report_action_error,
)
from app.routes.clients import (
    render_page,
    require_client_id,
    workspace_for,
)

from app.services.client_workspace import WorkspaceReadOnly
from app.services.proof_of_work import (
    ProofError,
    month_bounds,
)
from app.services.report_store import (
    ReportPreviewService,
    ReportValidation,
    check_notes,
)


router = APIRouter(tags=["report-previews"])


def _check_period(period):
    try:
        month_bounds(period)
    except (ProofError, ValueError, TypeError) as exc:
        raise HTTPException(
            404,
            "Invalid reporting month.",
        ) from exc


@router.post(
    "/clients/{client_id}/reports/{period}/preview",
    response_class=HTMLResponse,
)
async def report_preview(
    request: Request,
    client_id: str,
    period: str,
):
    require_client_id(client_id)
    _check_period(period)

    workspace = await workspace_for(request)

    if workspace is None:
        return RedirectResponse(
            "/start",
            status_code=303,
        )

    form = await read_report_form(request)

    try:
        snapshot = await run_in_threadpool(
            ReportPreviewService(workspace.store).build,
            client_id,
            period,
            form,
        )
    except (
        ReportValidation,
        WorkspaceReadOnly,
        FirestoreNotFound,
        FirestoreConflict,
        FirestoreUnavailable,
    ) as exc:
        return await render_report_action_error(
            request,
            workspace.store,
            client_id,
            period,
            {
                **form,
                "action": "new_revision",
            },
            exc,
        )

    return render_page(
        request,
        "report_preview.html",
        {
            "client_id": client_id,
            "period": period,
            "snapshot": snapshot,
            "notes": {
                **snapshot["writing"],
                "brand": snapshot["brand"],
            },
        },
    )


@router.post(
    "/clients/{client_id}/reports/{period}/preview/return",
    response_class=HTMLResponse,
)
async def return_to_report_editor(
    request: Request,
    client_id: str,
    period: str,
):
    """Return an unsaved preview to the editor with its original text."""

    require_client_id(client_id)
    _check_period(period)

    workspace = await workspace_for(request)

    if workspace is None:
        return RedirectResponse(
            "/start",
            status_code=303,
        )

    form = await read_report_form(request)

    try:
        notes = check_notes(form)

        context = await run_in_threadpool(
            _page_data,
            workspace.store,
            client_id,
            period,
        )
    except (
        ReportValidation,
        FirestoreNotFound,
        FirestoreConflict,
        FirestoreUnavailable,
    ) as exc:
        return await render_report_action_error(
            request,
            workspace.store,
            client_id,
            period,
            {
                **form,
                "action": "new_revision",
            },
            exc,
        )

    if not context["editable"]:
        raise HTTPException(
            403,
            "This report is read-only.",
        )

    return render_page(
        request,
        "report_workspace.html",
        {
            **context,
            "notes": notes,
            "new_link": "",
        },
    )
