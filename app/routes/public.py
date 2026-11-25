"""Public product, demonstration and informational pages."""

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageDraw, ImageFont

from app.core.config import settings
from app.demo.sample_data import SAMPLE


router = APIRouter()
TEMPLATES_DIRECTORY = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIRECTORY))


def context(request: Request, **kwargs):
    return {"request": request, "settings": settings, **kwargs}


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "home.html", context(request))


@router.get("/sample", response_class=HTMLResponse)
async def sample(request: Request):
    return templates.TemplateResponse(
        request, "sample.html", context(request, demo=SAMPLE),
    )


@router.get("/sample/workspace", response_class=HTMLResponse)
async def sample_workspace(request: Request):
    """Fictional demonstration; no customer data or storage."""
    return templates.TemplateResponse(
        request, "sample_workspace.html",
        context(request, demo=SAMPLE),
    )


def _font(size: int, *, bold: bool = False):
    filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(filename, size)
    except OSError:
        return ImageFont.load_default()


@lru_cache(maxsize=1)
def social_card_bytes() -> bytes:
    canvas = Image.new("RGB", (1200, 630), "#10151F")
    draw = ImageDraw.Draw(canvas)

    draw.rounded_rectangle(
        (68, 72, 1132, 558), radius=34,
        fill="#1C2635", outline="#3A5060", width=3,
    )
    draw.rounded_rectangle(
        (115, 122, 197, 204), radius=18, fill="#39C7B5",
    )
    draw.text(
        (140, 132), "R", fill="#10151F",
        font=_font(52, bold=True),
    )
    draw.text(
        (220, 131), "Reportlume", fill="#F2F5FA",
        font=_font(47, bold=True),
    )
    draw.text(
        (115, 271), "Client reporting, made clear.",
        fill="#F2F5FA", font=_font(43, bold=True),
    )
    draw.text(
        (115, 347), "Branded monthly X client reports",
        fill="#A8B5C6", font=_font(30),
    )
    draw.text(
        (115, 391), "for ghostwriters and content teams.",
        fill="#A8B5C6", font=_font(30),
    )
    draw.rounded_rectangle(
        (115, 481, 487, 519), radius=19, fill="#254844",
    )
    draw.text(
        (139, 486), "CSV  /  REPORTS  /  CLARITY",
        fill="#9BF0E3", font=_font(20, bold=True),
    )

    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


@router.get("/social-card.png", include_in_schema=False)
async def social_card():
    return Response(
        content=social_card_bytes(),
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return RedirectResponse("/static/favicon.svg", status_code=307)


@router.get("/robots.txt", include_in_schema=False)
async def robots():
    rules = "\n".join((
        "User-agent: *",
        "Disallow: /api/",
        "Disallow: /clients",
        "Disallow: /dashboard",
        "Disallow: /login",
        "Disallow: /signup",
        "Disallow: /settings/",
        "Disallow: /billing",
        "Disallow: /r/",
        "Disallow: /case-consent/",
        "Disallow: /cases/",
        "Disallow: /sample",
        "",
    ))
    return PlainTextResponse(
        rules,
        headers={"Cache-Control": "public, max-age=3600"},
    )



@router.get("/privacy", response_class=HTMLResponse)
@router.get("/security", response_class=HTMLResponse)
@router.get("/terms", response_class=HTMLResponse)
@router.get("/acceptable-use", response_class=HTMLResponse)
@router.get("/contact", response_class=HTMLResponse)
async def source_notes(request: Request):
    return templates.TemplateResponse(request, "source_notes.html", context(request))
