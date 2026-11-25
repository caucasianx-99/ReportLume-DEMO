
"""Public, bearer-scoped report review and consent pages.

HTML links have customer-safe HTML error pages.

A bearer link conveys access to one specific revision,
never an authenticated owner session.
"""
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.core.firestore_rest import (
    TenantStore,
    FirestoreNotFound,
    FirestoreUnavailable,
    FirestoreConflict,
)
from app.routes.auth import read_form
from app.routes.clients import render_page
from app.services.report_store import ReportValidation, ReportConflict
from app.services.client_workspace import WorkspaceReadOnly
from app.services.share_case import SharingService, CaseService
from app.services.share_activity import ShareActivityService
from app.services.report_pdf import pdf_bytes, case_pdf_bytes

router = APIRouter(tags=['public-review'])


def _public_store(uid):
    """Create a tenant-bound store without disclosing tenant data."""
    try:
        return TenantStore(uid)
    except (ValueError, FirestoreUnavailable) as exc:
        raise FirestoreNotFound(
            'Link unavailable'
        ) from exc


def _status(exc):
    if isinstance(exc, ReportValidation):
        return 400
    if isinstance(exc, WorkspaceReadOnly):
        return 403
    if isinstance(exc, (FirestoreConflict, ReportConflict)):
        return 409
    if isinstance(exc, (FirestoreNotFound, ValueError)):
        return 404
    return 503


def _public_error(request, exc):
    """Return HTML without exposing private record details."""
    status = _status(exc)
    messages = {
        400: (
            'Unable to process this request',
            'Check the submitted information.',
        ),
        403: (
            'Action not available',
            'This link does not allow that action.',
        ),
        404: (
            'Link unavailable',
            'This link may have expired or been revoked.',
        ),
        409: (
            'Please refresh',
            'The record changed. Reload the link and try again.',
        ),
        503: (
            'Temporarily unavailable',
            'The page could not be loaded right now.',
        ),
    }
    heading, message = messages[status]
    return render_page(
        request,
        'public_error.html',
        {
            'heading': heading,
            'message': message,
        },
        status_code=status,
    )


def _case_unavailable(request):
    """Private-data-free 404, dedicated to public case-study links.

    Do not reuse the private report-link error template: its instructions
    refer to a different, bearer-scoped workflow and can confuse readers.
    Never include client names, owner data, draft text or consent details.
    """
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow,noarchive">'
        '<meta name="referrer" content="no-referrer">'
        '<title>Case study unavailable</title>'
        '<link rel="stylesheet" href="/static/css/reporting.css">'
        '</head><body class="client-report-page">'
        '<main id="main-content" class="public-report-main">'
        '<section class="section"><div class="container report-wrap">'
        '<article class="report-paper report-error">'
        '<h1>This case study is no longer available</h1>'
        '<p>This public case study cannot currently be viewed.</p>'
        '</article></div></section></main></body></html>'
    )
    return HTMLResponse(
        content=page,
        status_code=404,
        headers={
            'Cache-Control': 'no-store',
            'X-Robots-Tag': 'noindex',
        },
    )


def _form_error(request, exc):
    """Map form and CSRF errors to the customer-safe HTML shell."""
    if isinstance(exc, HTTPException):
        if exc.status_code == 413:
            return render_page(
                request,
                'public_error.html',
                {
                    'heading': 'Form too large',
                    'message': (
                        'Shorten the response and submit it again.'
                    ),
                },
                status_code=413,
            )
        if exc.status_code in (400, 403, 415):
            return render_page(
                request,
                'public_error.html',
                {
                    'heading': 'Unable to submit feedback',
                    'message': (
                        'Refresh this page and check your response.'
                    ),
                },
                status_code=exc.status_code,
            )
        raise exc
    return _public_error(request, exc)


def _review_validation_fields(data, error):
    """Keep user-entered values and mark the field needing attention."""
    data = data if isinstance(data, dict) else {}
    action = data.get('action')
    fields = {}

    if action == 'approve' and data.get('confirm_approval') != 'on':
        fields['confirm_approval'] = (
            'Confirm approval before sending.'
        )

    if not str(data.get('reviewer_name') or '').strip():
        fields['reviewer_name'] = 'Enter your name.'

    if action == 'comment' and not str(
        data.get('comment') or ''
    ).strip():
        fields['comment'] = (
            'Describe the changes you are requesting.'
        )

    detail = str(error)
    if 'Reviewer name' in detail:
        fields['reviewer_name'] = detail
    elif 'Comment' in detail:
        fields['comment'] = detail

    return fields


async def _review_error_page(
    request,
    store,
    client_id,
    token,
    uid,
    data,
    exc,
):
    """Validation errors stay on the public report and keep values."""
    try:
        link, snapshot, _ = await run_in_threadpool(
            SharingService(store).verify,
            client_id,
            token,
        )
    except (
        FirestoreNotFound,
        FirestoreUnavailable,
        ReportValidation,
    ) as error:
        return _public_error(request, error)

    if link.get('role') != 'reviewer':
        return _public_error(
            request,
            WorkspaceReadOnly('Link is view only'),
        )

    # The public template receives only sanitized reviewer form values;
    # tenant settings, owner/client records and consent details stay private.
    safe = data if isinstance(data, dict) else {}
    return render_page(
        request,
        'report_view.html',
        {
            'snapshot': snapshot,
            'review': True,
            'share_action': (
                f'/r/{uid}/{client_id}/{token}/respond'
            ),
            'sent': False,
            'owner_back': '',
            'pdf_url': (
                f'/r/{uid}/{client_id}/{token}/pdf'
            ),
            'review_values': {
                'reviewer_name': str(
                    safe.get('reviewer_name', '')
                )[:80],
                'comment': str(
                    safe.get('comment', '')
                )[:2000],
                'confirm_approval': (
                    safe.get('confirm_approval') == 'on'
                ),
                'action': str(
                    safe.get('action', '')
                )[:20],
            },
            'review_field_errors': _review_validation_fields(
                safe, exc
            ),
            'review_form_error': (
                'Please correct the highlighted fields and try again.'
            ),
        },
        status_code=400,
    )


@router.get(
    '/r/{uid}/{client_id}/{token}',
    response_class=HTMLResponse,
)
async def shared_report(
    request: Request,
    uid: str,
    client_id: str,
    token: str,
    sent: int = 0,
):
    try:
        store = _public_store(uid)
        link, snapshot, version = await run_in_threadpool(
            SharingService(store).verify,
            client_id,
            token,
        )
    except (
        FirestoreNotFound,
        FirestoreUnavailable,
        ReportValidation,
        ValueError,
    ) as exc:
        return _public_error(request, exc)

    response = render_page(
        request,
        'report_view.html',
        {
            'snapshot': snapshot,
            'review': link.get('role') == 'reviewer',
            'share_action': (
                f'/r/{uid}/{client_id}/{token}/respond'
            ),
            'sent': sent == 1,
            'review_values': {},
            'review_field_errors': {},
            'review_form_error': '',
            'owner_back': '',
            'pdf_url': (
                f'/r/{uid}/{client_id}/{token}/pdf'
            ),
        },
    )

    await run_in_threadpool(
        ShareActivityService(store).record,
        client_id,
        token,
        link,
        version,
        'html',
    )
    return response


@router.post(
    '/r/{uid}/{client_id}/{token}/respond'
)
async def shared_response(
    request: Request,
    uid: str,
    client_id: str,
    token: str,
):
    data = {}
    try:
        store = _public_store(uid)
        data = await read_form(request)
        await run_in_threadpool(
            SharingService(store).submit,
            client_id,
            token,
            data,
        )
    except ReportValidation as exc:
        return await _review_error_page(
            request,
            store,
            client_id,
            token,
            uid,
            data,
            exc,
        )
    except (
        FirestoreNotFound,
        FirestoreUnavailable,
        FirestoreConflict,
        WorkspaceReadOnly,
        ReportConflict,
        ValueError,
        HTTPException,
    ) as exc:
        return _form_error(request, exc)

    return RedirectResponse(
        f'/r/{uid}/{client_id}/{token}?sent=1#review',
        status_code=303,
    )


@router.get('/r/{uid}/{client_id}/{token}/pdf')
async def shared_pdf(
    request: Request,
    uid: str,
    client_id: str,
    token: str,
):
    try:
        store = _public_store(uid)
        link, snapshot, version = await run_in_threadpool(
            SharingService(store).verify,
            client_id,
            token,
        )
        content = await run_in_threadpool(
            pdf_bytes, snapshot
        )
    except (
        FirestoreNotFound,
        FirestoreUnavailable,
        ReportValidation,
        ValueError,
    ) as exc:
        return _public_error(request, exc)

    period = snapshot.get('period', 'report')
    if (
        not isinstance(period, str)
        or not re.fullmatch(
            r'\d{4}-(0[1-9]|1[0-2])',
            period,
        )
    ):
        period = 'report'

    await run_in_threadpool(
        ShareActivityService(store).record,
        client_id,
        token,
        link,
        version,
        'pdf',
    )
    return Response(
        content,
        media_type='application/pdf',
        headers={
            'Cache-Control': 'no-store',
            'Content-Disposition': (
                f'attachment; filename="client-report-{period}.pdf"'
            ),
        },
    )


@router.get(
    '/case-consent/{uid}/{client_id}/{period}/{token}',
    response_class=HTMLResponse,
)
async def consent_page(
    request: Request,
    uid: str,
    client_id: str,
    period: str,
    token: str,
    saved: int = 0,
):
    try:
        store = _public_store(uid)
        case, _ = await run_in_threadpool(
            CaseService(store).verify_consent_link,
            client_id,
            period,
            token,
        )
    except (
        FirestoreNotFound,
        FirestoreUnavailable,
        ReportValidation,
        ValueError,
    ) as exc:
        return _public_error(request, exc)

    return render_page(
        request,
        'case_consent.html',
        {
            'case': case,
            'saved': saved == 1,
            'respond_url': (
                f'/case-consent/{uid}/{client_id}/{period}/'
                f'{token}/respond'
            ),
        },
    )


@router.post(
    '/case-consent/{uid}/{client_id}/{period}/{token}/respond'
)
async def consent_response(
    request: Request,
    uid: str,
    client_id: str,
    period: str,
    token: str,
):
    try:
        store = _public_store(uid)
        data = await read_form(request)
        await run_in_threadpool(
            CaseService(store).consent,
            client_id,
            period,
            token,
            data,
        )
    except (
        FirestoreNotFound,
        FirestoreUnavailable,
        FirestoreConflict,
        WorkspaceReadOnly,
        ReportValidation,
        ReportConflict,
        ValueError,
        HTTPException,
    ) as exc:
        return _form_error(request, exc)

    return RedirectResponse(
        f'/case-consent/{uid}/{client_id}/{period}/{token}?saved=1',
        status_code=303,
    )


@router.get(
    '/cases/{uid}/{client_id}/{period}',
    response_class=HTMLResponse,
)
async def public_case(
    request: Request,
    uid: str,
    client_id: str,
    period: str,
):
    try:
        store = _public_store(uid)
        case = await run_in_threadpool(
            CaseService(store).public,
            client_id,
            period,
        )
    except (FirestoreNotFound, WorkspaceReadOnly) as exc:
        return _case_unavailable(request)
    except (FirestoreUnavailable, ReportValidation, ValueError) as exc:
        return _public_error(request, exc)

    return render_page(
        request,
        'public_case.html',
        {
            'case': case,
            'pdf_url': (
                f'/cases/{uid}/{client_id}/{period}/pdf'
            ),
        },
    )


@router.get('/cases/{uid}/{client_id}/{period}/pdf')
async def public_case_pdf(
    request: Request,
    uid: str,
    client_id: str,
    period: str,
):
    try:
        store = _public_store(uid)
        case = await run_in_threadpool(
            CaseService(store).public,
            client_id,
            period,
        )
        content = await run_in_threadpool(
            case_pdf_bytes,
            case,
        )
    except (FirestoreNotFound, WorkspaceReadOnly) as exc:
        return _case_unavailable(request)
    except (FirestoreUnavailable, ReportValidation, ValueError) as exc:
        return _public_error(request, exc)

    return Response(
        content,
        media_type='application/pdf',
        headers={
            'Cache-Control': 'no-store',
            'Content-Disposition': (
                f'attachment; filename="case-study-{period}.pdf"'
            ),
        },
    )
