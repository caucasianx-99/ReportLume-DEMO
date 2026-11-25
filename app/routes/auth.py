"""Anonymous browser workspaces, no account or identity provider."""
from urllib.parse import parse_qs
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from app.core.config import settings
from app.core.csrf import attach_csrf_cookie, csrf_token_for, verify_csrf
from app.core.firestore_rest import FirestoreUnavailable, TenantStore
from app.core.rate_limit import check_auth_rate
from app.core.release_policy import customer_data_ready
from app.core.local_store import guest_for, new_guest, delete_guest, TTL
from app.routes.public import templates
from app.services.dashboard_overview import dashboard_summary
router=APIRouter()

async def read_form(request: Request) -> dict:
    """Read a small URL-encoded form with a hard byte limit."""

    content_type = request.headers.get(
        "content-type",
        "",
    ).split(";")[0].strip().lower()

    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(
            415,
            "Unsupported form type",
        )

    body = bytearray()

    async for chunk in request.stream():
        body.extend(chunk)

        if len(body) > 8192:
            raise HTTPException(
                413,
                "Form is too large",
            )

    try:
        parsed = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            max_num_fields=24,
            encoding="utf-8",
            errors="strict",
        )

    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(
            400,
            "Invalid form",
        ) from exc

    data = {
        key: values[-1]
        for key, values in parsed.items()
    }

    verify_csrf(
        request,
        data.get("csrf_token", ""),
    )

    return data


async def current_user(request: Request):
    uid=await run_in_threadpool(guest_for,request.cookies.get(settings.session_cookie_name,''))
    return {'uid':uid} if uid else None

@router.get('/start')
async def start(request: Request):
    if await current_user(request):
        return RedirectResponse('/dashboard',status_code=303)
    token=csrf_token_for(request)
    response=templates.TemplateResponse(request,'start.html',{'request':request,'settings':settings,'csrf_token':token})
    attach_csrf_cookie(response,token)
    return response

@router.post('/start')
async def start_workspace(request: Request):
    await read_form(request)
    if await current_user(request):
        return RedirectResponse('/dashboard',status_code=303)
    if not customer_data_ready(settings):
        raise HTTPException(503,"This deployment is not ready to accept customer data.")
    await check_auth_rate(request,"signup")
    try:
        token=await run_in_threadpool(new_guest)
    except RuntimeError as exc:
        raise HTTPException(503,'Workspace capacity reached; try again later.') from exc
    response=RedirectResponse('/dashboard',status_code=303)
    response.set_cookie(settings.session_cookie_name,token,max_age=TTL,httponly=True,secure=settings.production,samesite='lax',path='/')
    return response

@router.get(
    "/dashboard",
    response_class=HTMLResponse,
)
async def dashboard(request: Request):
    """Render a bounded summary of clients from the verified account only."""

    try:
        claims = await current_user(request)

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Workspace unavailable",
        )

    if claims is None:
        return RedirectResponse(
            "/start",
            status_code=303,
        )

    try:
        store = TenantStore(
            claims["uid"]
        )

        overview = await run_in_threadpool(
            dashboard_summary,
            store,
        )

    except (
        FirestoreUnavailable,
        ValueError,
        KeyError,
    ):
        raise HTTPException(
            status_code=503,
            detail="Workspace data unavailable",
        )

    token = csrf_token_for(request)

    response = templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "settings": settings,
            "profile": overview["profile"],
            "overview": overview,
            "signed_in": True,
            "csrf_token": token,
        },
    )

    attach_csrf_cookie(
        response,
        token,
    )

    response.headers["Cache-Control"] = "no-store"

    return response


@router.post('/workspace/reset')
async def reset(request: Request):
    data=await read_form(request)
    if data.get("confirm_reset") != "yes":
        raise HTTPException(400,"Confirm workspace deletion first.")
    claims=await current_user(request)
    if claims:
        await run_in_threadpool(delete_guest,claims['uid'])
    response=RedirectResponse('/',status_code=303)
    response.delete_cookie(settings.session_cookie_name,path='/',secure=settings.production,httponly=True,samesite='lax')
    return response
