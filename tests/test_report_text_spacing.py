"""Rendered line-preserved text starts at the first actual authored character."""

import copy
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.demo.sample_data import SAMPLE
from app.routes.public import templates


CLIENT = TestClient(app)


def test_shared_report_renderer_keeps_real_linebreaks_without_padding():
    snapshot = copy.deepcopy(SAMPLE["snapshot"])

    snapshot["writing"]["executive_summary"] = (
        "October summary\nSecond line"
    )
    snapshot["writing"]["next_month_plan"] = "November plan"
    snapshot["writing"]["client_message"] = "Message to customer"
    snapshot["top_posts"][0]["post_text"] = (
        "Post beginning\nMore text"
    )

    html = templates.get_template(
        "report_body.html"
    ).render(snapshot=snapshot)

    assert (
        '<p class="preserve-lines">'
        'October summary\nSecond line</p>'
    ) in html

    assert (
        '<p class="preserve-lines">'
        'November plan</p>'
    ) in html

    assert (
        '<p class="preserve-lines">'
        'Message to customer</p>'
    ) in html

    assert (
        '<p class="preserve-lines">'
        'Post beginning\nMore text</p>'
    ) in html

    assert not re.search(
        r'<p class="preserve-lines">\s+October',
        html,
    )
    assert not re.search(
        r'<p class="preserve-lines">\s+November',
        html,
    )


def test_public_demo_uses_same_spacing_fixes():
    response = CLIENT.get("/sample")

    assert response.status_code == 200

    text = (
        SAMPLE["snapshot"]["writing"]["executive_summary"]
    )

    assert (
        f'<p class="preserve-lines">{text}</p>'
        in response.text
    )


def test_classification_post_text_no_extra_newline():
    from starlette.requests import Request
    from app.core.config import settings

    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/clients/a/posts",
        "raw_path": b"/clients/a/posts",
        "query_string": b"",
        "headers": [],
        "app": app,
        "scheme": "http",
        "server": ("localhost", 80),
    })

    html = templates.get_template(
        "classification.html"
    ).render(
        request=request,
        settings=settings,
        csrf_token="synthetic-token",
        client={
            "id": "a",
            "display_name": "Example client",
        },
        period="2026-10",
        previous_period="2026-09",
        posts=[{
            "post_id": "1234567890123456789",
            "version": "version1",
            "authorship": "Ghostwriter",
            "content_type": "Post",
            "topic": "Example",
            "format": "Educational",
            "published_date": "2026-10-05",
            "post_text": "Start immediately\nNext line",
            "possible_reply": False,
        }],
        editable=True,
        excluded_processing=0,
        next_page_token="",
    )

    assert (
        '<p class="classification-post-text">'
        'Start immediately\nNext line</p>'
    ) in html


def test_private_notes_render_without_template_indentation():
    detail = (
        Path(__file__).resolve().parents[1]
        / "app/templates/client_detail.html"
    ).read_text(encoding="utf-8")

    proof = (
        Path(__file__).resolve().parents[1]
        / "app/templates/proof_of_work.html"
    ).read_text(encoding="utf-8")

    assert (
        '<dd class="preserve-lines">'
        '{{ client.get("goal_notes")'
    ) in detail

    assert (
        '<dd class="preserve-lines">'
        '{{ client.get("notes")'
    ) in detail

    assert (
        '<p class="preserve-lines">'
        '{{ manual.delivery_notes'
    ) in proof

    assert (
        '>\n          {{ manual.delivery_notes'
        not in proof
    )


def test_date_macros_preserve_machine_readable_values():
    labels = templates.get_template(
        "_date_display.html"
    ).module

    assert (
        str(labels.date_label("2026-11-04"))
        == "November 4, 2026"
    )

    assert (
        str(labels.date_label("2026-11-04T11:17:00+04:00"))
        == "November 4, 2026"
    )

    assert (
        str(labels.month_label("2026-10"))
        == "October 2026"
    )

    assert str(labels.date_label("Not set")) == "Not set"
    assert str(labels.month_label("Unknown")) == "Unknown"
    assert str(labels.date_label("2026-99-99")) == "2026-99-99"
    assert str(labels.month_label("2026-99")) == "2026-99"

    source = (
        Path(__file__).resolve().parents[1]
        / "app/templates/import_csv.html"
    ).read_text(encoding="utf-8")

    assert 'type="date"' in source
    assert 'id="measurement-date"' in source

    assert (
        "date_label(item.get('measurement_date', 'Unknown'))"
        in source
        or 'date_label(item.get("measurement_date", ""))'
        in source
    )
