"""Authenticated report revisions, publishing, sharing and case-study actions.

Browser routes keep the owner workspace separate from customer-facing pages.
Frozen report revisions are never updated after creation.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import quote, parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.http_errors import owner_feedback
from app.core.firestore_rest import (
    FirestoreConflict,
    FirestoreNotFound,
    FirestoreUnavailable,
)
from app.core.csrf import verify_csrf
from app.routes.clients import (
    render_page,
    require_client_id,
    workspace_for,
)
from app.services.client_workspace import (
    WorkspaceReadOnly,
    require_write_access,
    has_paid_access,
    parse_timestamp,
    selection_required,
)
from app.services.proof_of_work import month_bounds, ProofError
from app.services.monthly_defaults import (
    previous_completed_month,
    is_unfinished_month,
)
from app.services.publication_readiness import publication_readiness
from app.services.report_store import (
    ReportConflict,
    ReportValidation,
    ReportStore,
)
from app.services.share_case import SharingService, CaseService
from app.services.alerts import AlertService
from app.services.workspace_access import (
    can_publish_unmarked_copy,
    plan_entitlements,
)
from app.services.report_pdf import pdf_bytes, case_pdf_bytes


router = APIRouter(tags=["reports"])

MAX_REPORT_FORM_BYTES = 32 * 1024
MAX_REPORT_FORM_FIELDS = 32


async def read_report_form(request: Request) -> dict:
    """Parse a bounded report form and validate double-submit CSRF."""
    content_type = request.headers.get("content-type", "")
    content_type = content_type.split(";", 1)[0].strip().lower()

    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Unsupported report form type.")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_REPORT_FORM_BYTES:
            raise HTTPException(
                413,
                "Report form is too large. Shorten the text and try again.",
            )

    try:
        values = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=MAX_REPORT_FORM_FIELDS,
            encoding="utf-8",
            errors="strict",
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Invalid report form.") from exc

    if any(len(items) != 1 for items in values.values()):
        raise HTTPException(400, "Duplicate report form field.")

    result = {key: items[0] for key, items in values.items()}
    verify_csrf(request, result.get("csrf_token", ""))
    return result


def _period(period):
    try:
        month_bounds(period)
    except ProofError as exc:
        raise HTTPException(404, "Invalid reporting month") from exc


async def _owner(request):
    workspace = await workspace_for(request)
    if workspace is None:
        raise HTTPException(401, "Sign in required")
    return workspace.store


def _error(exc):
    if isinstance(exc, (ReportValidation, ProofError)):
        raise HTTPException(400, str(exc)) from exc
    if isinstance(exc, WorkspaceReadOnly):
        raise HTTPException(403, str(exc)) from exc
    if isinstance(exc, (ReportConflict, FirestoreConflict)):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, FirestoreNotFound):
        raise HTTPException(404, "Record not found") from exc
    raise HTTPException(
        503, "Reporting storage temporarily unavailable"
    ) from exc


def _editable(store, client_id, client):
    profile, _ = store.get_user_with_version()
    try:
        require_write_access(profile)
    except WorkspaceReadOnly:
        return False

    return (
        client.get("status") == "active"
        and (
            has_paid_access(profile)
            or profile.get("unpaid_client_id") == client_id
        )
    )


def _previous_published_plan(reports, client_id, period):
    """Offer the prior month's published plan without inserting it."""
    start, _ = month_bounds(period)
    previous_period = (start - timedelta(days=1)).strftime("%Y-%m")
    previous_parent, _ = reports.get_parent(client_id, previous_period)
    revision = previous_parent.get("published_revision")

    if not revision:
        return ("", "")

    _, previous_snapshot = reports.get_revision(
        client_id, previous_period, revision
    )
    writing = previous_snapshot.get("writing", {})
    if not isinstance(writing, dict):
        return ("", "")

    return (
        previous_period,
        str(writing.get("next_month_plan") or ""),
    )


def _page_data(store, client_id, period):
    reports = ReportStore(store)
    shares = SharingService(store)
    cases = CaseService(store)

    client = store.get_client(client_id)
    parent, _ = reports.get_parent(client_id, period)
    latest = parent.get("latest_revision")
    published_id = parent.get("published_revision")

    latest_fields = {}
    snapshot = None

    if latest:
        latest_fields, snapshot = reports.get_revision(
            client_id, period, latest
        )

    published_fields = {}
    published_snapshot = None

    if published_id:
        if published_id == latest and snapshot is not None:
            published_fields = latest_fields
            published_snapshot = snapshot
        else:
            published_fields, published_snapshot = reports.get_revision(
                client_id, period, published_id
            )

    revisions, revisions_more = reports.list_revisions(
        client_id, period
    )

    copy_sources = {
        item["copied_from_revision_id"]
        for item in revisions
        if item.get("copied_from_revision_id")
    }
    numbers = {
        item["id"]: item["version_number"]
        for item in revisions
        if "version_number" in item
    }

    for item in revisions:
        copied_from = item.get("copied_from_revision_id")
        if copied_from in numbers:
            item["source_version_label"] = (
                f"Version {numbers[copied_from]}"
            )

    all_month_links = shares.month_links(client_id, period)
    has_active_source_links = any(
        item.get("revision_id") in copy_sources
        and item.get("link_state") == "Active"
        for item in all_month_links
    )

    links = all_month_links[:50]
    links_more = len(all_month_links) > 50

    for item in links:
        value = numbers.get(item.get("revision_id"))
        item["version_label"] = (
            f"Version {value}" if value else "Older version"
        )

    feedback, reviews_more = shares.feedback(client_id, period)
    feedback_groups_by_id = {}

    for item in feedback:
        revision_id = item.get("revision_id")
        group = feedback_groups_by_id.setdefault(
            revision_id,
            {
                "revision_id": revision_id,
                "version_label": (
                    f"Version {numbers[revision_id]}"
                    if revision_id in numbers
                    else "Older version"
                ),
                "responses": [],
            },
        )
        group["responses"].append(item)

    feedback_groups = sorted(
        feedback_groups_by_id.values(),
        key=lambda group: numbers.get(group["revision_id"], 0),
        reverse=True,
    )

    for group in feedback_groups:
        group["count"] = len(group["responses"])

    case, case_version = cases.get(client_id, period)
    _, history, alerts = AlertService(store).for_client(client_id)

    brand_profile, _ = store.get_user_with_version()
    case_visibility = cases.visibility(
        client_id, period, case=case, profile=brand_profile
    )
    plan_rights = plan_entitlements(brand_profile, settings)

    case_can_retain = bool(
        case.get("state") == "published"
        and case.get("draft_hash")
        and case.get("consent_hash") == case.get("draft_hash")
        and plan_rights["active"]
        and plan_rights["plan"] in ("solo", "professional", "agency")
        and not cases._trial_case_withdrawn(
            case,
            brand_profile,
            current=datetime.now(timezone.utc),
        )
    )
    case_retained = bool(
        brand_profile.get("retained_case_client_id") == client_id
        and brand_profile.get("retained_case_period") == period
    )
    case_auto_public_on_solo = bool(
        case_can_retain
        and plan_rights["plan"] == "solo"
        and case_visibility["public"]
        and not case_retained
    )

    can_copy = can_publish_unmarked_copy(brand_profile, settings)
    paid_copy_source_id = ""

    if can_copy:
        if (
            published_id
            and published_snapshot
            and published_snapshot.get("trial_watermark") is True
        ):
            paid_copy_source_id = published_id
        elif (
            latest
            and snapshot
            and snapshot.get("trial_watermark") is True
        ):
            paid_copy_source_id = latest

    paid_copy_source_is_latest = bool(
        paid_copy_source_id and paid_copy_source_id == latest
    )
    can_publish_paid_copy = bool(paid_copy_source_id)

    show_paid_copy_panel = (
        can_publish_paid_copy
        and not (
            paid_copy_source_is_latest
            and latest != published_id
        )
    )
    latest_will_publish_unmarked = bool(
        can_copy
        and latest != published_id
        and snapshot
        and snapshot.get("trial_watermark") is True
    )

    if paid_copy_source_id in numbers:
        paid_copy_source_label = (
            f"Version {numbers[paid_copy_source_id]}"
        )
    elif paid_copy_source_id == published_id and paid_copy_source_id:
        paid_copy_source_label = "Published version"
    elif paid_copy_source_id:
        paid_copy_source_label = "Latest saved version"
    else:
        paid_copy_source_label = ""

    trial_start = parse_timestamp(
        brand_profile.get("trial_started_at")
    )
    trial_end = parse_timestamp(
        brand_profile.get("trial_ends_at")
    )
    now_utc = datetime.now(timezone.utc)

    show_trial_link_warning = (
        not bool(brand_profile.get("ever_paid"))
        and not has_paid_access(brand_profile)
        and trial_start is not None
        and trial_end is not None
        and trial_start <= now_utc < trial_end
    )

    if snapshot:
        notes = {
            "brand": snapshot.get("brand") or "Your Agency",
            **(snapshot.get("writing") or {}),
        }
        previous_period, previous_plan = ("", "")
    else:
        notes = {
            "brand": brand_profile.get("brand_name") or "Your Agency",
            "executive_summary": "",
            "next_month_plan": "",
            "client_message": "",
        }
        previous_period, previous_plan = _previous_published_plan(
            reports, client_id, period
        )

    return {
        "client": client,
        "period": period,
        "parent": parent,
        "latest": latest,
        "snapshot": snapshot,
        "publication_checklist": (
            publication_readiness(snapshot) if snapshot else None
        ),
        "notes": notes,
        "previous_period": previous_period,
        "previous_plan": previous_plan,
        "feedback": feedback,
        "feedback_groups": feedback_groups,
        "reviews_more": reviews_more,
        "links": links,
        "links_more": links_more,
        "revisions": revisions,
        "latest_version_label": (
            f"Version {numbers[latest]}"
            if latest in numbers
            else "Latest saved version"
            if latest
            else "None"
        ),
        "published_version_label": (
            f"Version {numbers[published_id]}"
            if published_id in numbers
            else "Published version"
            if published_id
            else "Not published"
        ),
        "published_copy_from_label": next(
            (
                item.get("source_version_label", "")
                for item in revisions
                if item["id"] == published_id
            ),
            "",
        ),
        "incomplete_month": is_unfinished_month(period),
        "trial_report_available": (
            not has_paid_access(brand_profile)
            and not brand_profile.get("trial_report_client_id")
            and not brand_profile.get("ever_paid", False)
        ),
        "trial_report_used": bool(
            brand_profile.get("trial_report_client_id")
        ),
        "can_publish_paid_copy": can_publish_paid_copy,
        "show_paid_copy_panel": show_paid_copy_panel,
        "paid_copy_source_id": paid_copy_source_id,
        "paid_copy_source_label": paid_copy_source_label,
        "paid_copy_source_is_latest": paid_copy_source_is_latest,
        "latest_will_publish_unmarked": latest_will_publish_unmarked,
        "has_active_source_links": has_active_source_links,
        "trial_ends_at": brand_profile.get("trial_ends_at"),
        "show_trial_link_warning": show_trial_link_warning,
        "revisions_more": revisions_more,
        "case": case,
        "case_version": case_version,
        "case_visibility": case_visibility,
        "case_can_retain": case_can_retain,
        "case_retained": case_retained,
        "case_auto_public_on_solo": case_auto_public_on_solo,
        "client_selection_required": selection_required(brand_profile),
        "history": history,
        "alerts": alerts,
        "editable": _editable(store, client_id, client),
    }


def _form_error_code(exc):
    if isinstance(exc, (ReportValidation, ProofError)):
        return 400
    if isinstance(exc, WorkspaceReadOnly):
        return 403
    if isinstance(exc, (ReportConflict, FirestoreConflict)):
        return 409
    if isinstance(exc, FirestoreNotFound):
        return 404
    return 503


async def render_report_action_error(
    request, store, client_id, period, form, error
):
    """Show an owner-safe error and retain report form input."""
    status = _form_error_code(error)
    feedback = owner_feedback(status, str(error))

    if (
        isinstance(error, ReportValidation)
        and str(error) == "Publishing checklist confirmation is required."
    ):
        feedback = type(feedback)(
            "Review the publishing checklist and check the "
            "confirmation box before publishing this version."
        )

    try:
        context = await run_in_threadpool(
            _page_data, store, client_id, period
        )
    except (
        FirestoreNotFound,
        FirestoreUnavailable,
        FirestoreConflict,
        ReportValidation,
    ) as exc:
        _error(exc)

    action = form.get("action", "")

    if action == "new_revision":
        current = dict(context["notes"])
        for field in (
            "brand",
            "executive_summary",
            "next_month_plan",
            "client_message",
        ):
            supplied = form.get(field)
            if isinstance(supplied, str):
                current[field] = supplied
        context["notes"] = current

    return render_page(
        request,
        "report_workspace.html",
        {
            **context,
            "new_link": "",
            "form_error": feedback.message,
            "form_error_billing": feedback.billing_link,
            "form_error_action": action,
        },
        status_code=status,
    )


def _apply_action(store, client_id, period, data):
    action = data.get("action")
    reports = ReportStore(store)
    shares = SharingService(store)
    cases = CaseService(store)

    if action == "new_revision":
        revision = reports.create_revision(client_id, period, data)
        return (None, revision)

    if action == "publish":
        revision = data.get("revision", "")
        parent, _ = reports.get_parent(client_id, period)

        if parent.get("latest_revision") != revision:
            raise ReportConflict(
                "Report changed. Refresh and retry."
            )

        if data.get("confirm_report_review") != "on":
            raise ReportValidation(
                "Publishing checklist confirmation is required."
            )

        reports.publish(
            client_id,
            period,
            revision,
            confirm_incomplete_month=(
                data.get("confirm_incomplete_month") == "on"
            ),
            confirm_trial_use=(
                data.get("confirm_trial_use") == "on"
            ),
        )

    elif action == "publish_paid_copy":
        if data.get("confirm_paid_copy") != "on":
            raise ReportValidation(
                "Confirm that you want to publish a new paid version."
            )

        return (
            None,
            reports.publish_paid_copy(
                client_id,
                period,
                source_revision=data.get("revision") or None,
            ),
        )

    elif action == "new_share":
        role = data.get("role", "viewer")
        token = shares.create_link(client_id, period, role)
        return (f"/r/{store.uid}/{client_id}/{token}", None)

    elif action == "revoke_share":
        shares.revoke(client_id, data.get("share_id", ""))

    elif action == "case_save":
        cases.save(
            client_id,
            period,
            data,
            data.get("case_version", ""),
        )

    elif action == "case_link":
        token = cases.make_consent_link(client_id, period)
        return (
            f"/case-consent/{store.uid}/{client_id}/{period}/{token}",
            None,
        )

    elif action == "case_publish":
        cases.publish(client_id, period)

    elif action == "case_unpublish":
        cases.unpublish(client_id, period)

    elif action == "case_retain":
        cases.retain(client_id, period)

    else:
        raise ReportValidation("Unknown report action.")

    return (None, None)


@router.get("/clients/{client_id}/reports")
async def select_reporting_month(
    request: Request, client_id: str, period: str = ""
):
    require_client_id(client_id)
    store = await _owner(request)

    try:
        await run_in_threadpool(store.get_client, client_id)
    except (FirestoreNotFound, FirestoreUnavailable) as exc:
        _error(exc)

    if not period:
        period = previous_completed_month()
    _period(period)

    return RedirectResponse(
        f"/clients/{client_id}/reports/{period}",
        status_code=303,
    )


@router.get("/clients/{client_id}/reports/current")
async def current_reporting_month(
    request: Request, client_id: str
):
    require_client_id(client_id)
    store = await _owner(request)

    try:
        await run_in_threadpool(store.get_client, client_id)
    except (FirestoreNotFound, FirestoreUnavailable) as exc:
        _error(exc)

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return RedirectResponse(
        f"/clients/{client_id}/reports/{month}",
        status_code=303,
    )


@router.get(
    "/clients/{client_id}/reports/{period}",
    response_class=HTMLResponse,
)
async def report_workspace(
    request: Request,
    client_id: str,
    period: str,
    notice: str = "",
):
    require_client_id(client_id)
    _period(period)
    store = await _owner(request)

    try:
        context = await run_in_threadpool(
            _page_data, store, client_id, period
        )
    except (
        FirestoreNotFound,
        FirestoreUnavailable,
        FirestoreConflict,
        ReportValidation,
    ) as exc:
        _error(exc)

    allowed_notices = {
        "saved",
        "published",
        "unmarked",
        "revoked",
        "case_saved",
        "case_published",
        "case_unpublished",
        "case_retained",
    }

    return render_page(
        request,
        "report_workspace.html",
        {
            **context,
            "new_link": "",
            "action_notice": (
                notice if notice in allowed_notices else ""
            ),
        },
    )


@router.post(
    "/clients/{client_id}/reports/{period}/action",
    response_class=HTMLResponse,
)
async def report_action(
    request: Request, client_id: str, period: str
):
    require_client_id(client_id)
    _period(period)
    store = await _owner(request)
    data = await read_report_form(request)

    try:
        link, revision = await run_in_threadpool(
            _apply_action, store, client_id, period, data
        )

        if link:
            context = await run_in_threadpool(
                _page_data, store, client_id, period
            )
            return render_page(
                request,
                "report_workspace.html",
                {
                    **context,
                    "new_link": (
                        settings.base_url.rstrip("/") + link
                    ),
                    "action_notice": "link_created",
                },
            )

    except (
        ReportValidation,
        ProofError,
        ReportConflict,
        WorkspaceReadOnly,
        FirestoreConflict,
        FirestoreNotFound,
        FirestoreUnavailable,
    ) as exc:
        return await render_report_action_error(
            request,
            store,
            client_id,
            period,
            data,
            exc,
        )

    notices = {
        "new_revision": "saved",
        "publish": "published",
        "publish_paid_copy": "unmarked",
        "revoke_share": "revoked",
        "case_save": "case_saved",
        "case_publish": "case_published",
        "case_unpublish": "case_unpublished",
        "case_retain": "case_retained",
    }

    notice = notices.get(data.get("action"), "")
    suffix = f"?notice={notice}" if notice else ""

    if revision:
        suffix += (
            ("&" if suffix else "?")
            + f"revision={quote(revision)}"
        )

    return RedirectResponse(
        f"/clients/{client_id}/reports/{period}{suffix}",
        status_code=303,
    )


@router.get(
    "/clients/{client_id}/reports/{period}/revisions/{revision}",
    response_class=HTMLResponse,
)
async def owner_report(
    request: Request,
    client_id: str,
    period: str,
    revision: str,
):
    require_client_id(client_id)
    _period(period)
    store = await _owner(request)

    try:
        _, snapshot = await run_in_threadpool(
            ReportStore(store).get_revision,
            client_id,
            period,
            revision,
        )
    except (
        ReportValidation,
        FirestoreNotFound,
        FirestoreUnavailable,
    ) as exc:
        _error(exc)

    return render_page(
        request,
        "report_view.html",
        {
            "snapshot": snapshot,
            "review": False,
            "share_action": "",
            "sent": False,
            "owner_back": (
                f"/clients/{client_id}/reports/{period}"
            ),
            "pdf_url": (
                f"/clients/{client_id}/reports/{period}"
                f"/revisions/{revision}/pdf"
            ),
        },
    )


@router.get(
    "/clients/{client_id}/reports/{period}/revisions/{revision}/pdf"
)
async def owner_pdf(
    request: Request,
    client_id: str,
    period: str,
    revision: str,
):
    require_client_id(client_id)
    _period(period)
    store = await _owner(request)

    try:
        _, snapshot = await run_in_threadpool(
            ReportStore(store).get_revision,
            client_id,
            period,
            revision,
        )
        content = await run_in_threadpool(pdf_bytes, snapshot)
    except (
        ReportValidation,
        FirestoreNotFound,
        FirestoreUnavailable,
    ) as exc:
        _error(exc)

    return Response(
        content,
        media_type="application/pdf",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="client-report-{period}-{revision[:8]}.pdf"'
            ),
        },
    )


@router.get(
    "/clients/{client_id}/attention",
    response_class=HTMLResponse,
)
async def attention_page(
    request: Request, client_id: str
):
    require_client_id(client_id)
    store = await _owner(request)

    try:
        client, history, alerts = await run_in_threadpool(
            AlertService(store).for_client,
            client_id,
        )
    except (
        FirestoreNotFound,
        FirestoreUnavailable,
    ) as exc:
        _error(exc)

    return render_page(
        request,
        "attention.html",
        {
            "client": client,
            "history": history,
            "alerts": alerts,
        },
    )


@router.get("/clients/{client_id}/cases/{period}/pdf")
async def owner_case_pdf(
    request: Request,
    client_id: str,
    period: str,
):
    """Owners can export a case draft, visibly marked as a draft."""
    require_client_id(client_id)
    _period(period)
    store = await _owner(request)

    try:
        case, _ = await run_in_threadpool(
            CaseService(store).get,
            client_id,
            period,
        )
        if not case.get("draft_hash"):
            raise FirestoreNotFound("Case draft missing")

        content = await run_in_threadpool(
            case_pdf_bytes,
            case,
            draft=case.get("state") != "published",
        )
    except (
        FirestoreNotFound,
        FirestoreUnavailable,
        ReportValidation,
    ) as exc:
        _error(exc)

    return Response(
        content,
        media_type="application/pdf",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="case-study-{period}.pdf"'
            ),
        },
    )
