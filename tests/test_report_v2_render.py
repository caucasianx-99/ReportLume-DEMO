"""Regression coverage for published metadata and customer-safe templates."""

import hashlib
import json

from datetime import date
from pathlib import Path

import pytest

from jinja2 import (
    Environment,
    FileSystemLoader,
    select_autoescape,
)

from app.core.firestore_rest import (
    FirestoreNotFound,
    encode_fields,
    decode_fields,
)

from app.services.alerts import evaluate_alerts

from app.services.report_store import (
    ReportStore,
    check_notes,
)


CLIENT_ID = "client-a"

UID = "test-owner"

ROOT = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "templates"
)


class FakeStore:
    uid = UID

    user_name = (
        f"projects/test/databases/(default)/documents/users/{UID}"
    )

    def __init__(self):
        self.docs = {}
        self.sequence = 0
        self.commit_calls = []

        self.seed(
            self.user_name,
            {
                "unpaid_client_id": CLIENT_ID,
                "trial_started_at": "2026-10-10T00:00:00Z",
                "trial_ends_at": "2099-01-01T00:00:00Z",
            },
        )

        self.seed(
            self.client_name(CLIENT_ID),
            {
                "status": "active",
            },
        )

    def client_name(self, client_id):
        return (
            f"{self.user_name}/clients/{client_id}"
        )

    def seed(self, name, values):
        self.sequence += 1

        self.docs[name] = {
            "name": name,
            "fields": encode_fields(values),
            "updateTime": (
                f"2030-01-01T00:00:{self.sequence:02d}Z"
            ),
        }

    def get_document(self, name):
        if name not in self.docs:
            raise FirestoreNotFound(
                "Missing document"
            )

        return self.docs[name]

    def get_client(self, client_id):
        doc = self.get_document(
            self.client_name(client_id)
        )

        return {
            **decode_fields(doc["fields"]),
            "_update_time": doc["updateTime"],
        }

    def get_user_with_version(self):
        doc = self.get_document(
            self.user_name
        )

        return (
            decode_fields(doc["fields"]),
            doc["updateTime"],
        )

    def commit(self, writes):
        self.commit_calls.append(writes)

        for write in writes:
            doc = write["update"]
            name = doc["name"]

            if name in self.docs:
                old = decode_fields(
                    self.docs[name]["fields"]
                )
            else:
                old = {}

            values = decode_fields(
                doc["fields"]
            )

            if "updateMask" in write:
                updated_values = {
                    **old,
                    **values,
                }
            else:
                updated_values = values

            self.seed(
                name,
                updated_values,
            )

        return {
            "writeResults": [
                {}
                for _ in writes
            ],
        }


def snapshot(schema=2):
    observed = {
        "post_count": 4,
        "impressions": {
            "n": 4,
            "median": 18,
            "total": 75,
            "coverage": "4/4",
        },
        "engagements": {
            "n": 4,
            "median": 3,
            "total": 12,
            "coverage": "4/4",
        },
        "metrics": {
            "likes": {
                "total": 5,
                "coverage": "4/4",
            },
        },
    }

    return {
        "schema_version": schema,
        "period": "2026-10",
        "client": {
            "display_name": "Fictional Client",
        },
        "brand": "Test Agency",
        "window": {
            "start": "2026-10-01",
            "end": "2026-10-31",
        },
        "writing": {
            "executive_summary": "Clear summary",
            "next_month_plan": "A plan",
            "client_message": "Hello",
        },
        "observed": observed,
        "proof": {
            "deliverables": [],
            "comparison": {
                "status": "not_comparable",
                "reason": "Small sample",
            },
        },
        "quality": {
            "comparable_cohort": False,
        },
        "breakdown": {
            "authorship_content_type": [],
            "not_categorized_count": 0,
        },
        "charts": {
            "daily_posts": [
                {
                    "published_date": "2026-10-01",
                    "post_count": 2,
                },
                {
                    "published_date": "2026-10-02",
                    "post_count": 2,
                },
            ],
        },
        "topics": [],
        "formats": [],
        "top_posts": [],
        "goal": {
            "target_post_impressions": None,
            "observed_median_post_impressions": 18,
        },
        "since_started": {
            "collaboration_started": "2026-09-01",
            "published_report_history": [],
            "history_truncated": False,
        },
        "measurement": {
            "dates": [
                "2026-11-03",
            ],
        },
    }


def render_body(data):
    env = Environment(
        loader=FileSystemLoader(
            str(ROOT)
        ),
        autoescape=select_autoescape(
            ["html", "xml"]
        ),
    )

    return env.get_template(
        "report_body.html"
    ).render(
        snapshot=data
    )


def test_v2_html_contains_snapshot_svg_and_no_internal_labels():
    output = render_body(
        snapshot()
    )

    assert "<svg" in output
    assert "October 2026" in output
    assert "75" in output
    assert "4/4 posts with data" in output

    forbidden_labels = (
        "Reportlume",
        "x_analytics",
        "ghostwriter_input",
        "Unknown classifications",
        "In development",
    )

    for forbidden in forbidden_labels:
        assert forbidden not in output


def test_legacy_v1_snapshot_still_renders():
    legacy = snapshot(1)

    legacy.pop("charts")
    legacy.pop("breakdown")

    legacy["observed"] = {
        "impressions": {
            "n": 6,
            "median": 120,
        },
        "engagements": {
            "n": 6,
            "median": 8,
        },
    }

    output = render_body(
        legacy
    )

    assert "Fictional Client" in output
    assert "120" in output
    assert "<svg" not in output


def test_brand_defaults_to_agency_not_saas_operator():
    assert check_notes({})["brand"] == "Your Agency"


def test_published_summary_and_dashboard_marker_preserve_revision():
    store = FakeStore()

    reports = ReportStore(
        store
    )

    revision = "a" * 32
    data = snapshot()

    raw = json.dumps(
        data,
        sort_keys=True,
    )

    store.seed(
        reports._name(
            CLIENT_ID,
            "2026-10",
        ),
        {
            "period": "2026-10",
            "latest_revision": revision,
        },
    )

    store.seed(
        reports._revision_name(
            CLIENT_ID,
            "2026-10",
            revision,
        ),
        {
            "snapshot_json": raw,
            "sha256": hashlib.sha256(
                raw.encode()
            ).hexdigest(),
        },
    )

    reports.publish(
        CLIENT_ID,
        "2026-10",
        revision,
        confirm_trial_use=True,
    )

    parent, _ = reports.get_parent(
        CLIENT_ID,
        "2026-10",
    )

    summary = json.loads(
        parent["published_summary_json"]
    )

    assert summary["impressions_coverage"] == "4/4"
    assert summary["schema_version"] == 2

    assert (
        store.get_client(CLIENT_ID)["last_published_period"]
        == "2026-10"
    )

    assert reports.get_revision(
        CLIENT_ID,
        "2026-10",
        revision,
    )[1] == data


def test_two_month_early_signal_only_if_qualified():
    def item(period, median, age=15):
        return {
            "period": period,
            "median_impressions": median,
            "quality": {
                "comparable_cohort": True,
                "sample_size": 8,
                "median_age_days": age,
                "age_quartiles": [
                    13,
                    17,
                ],
            },
        }

    history = [
        item("2026-09", 100),
        item("2026-10", 70),
    ]

    alerts = evaluate_alerts(
        {
            "status": "paused",
        },
        history,
        date(2026, 11, 4),
    )

    assert any(
        alert["kind"] == "early_signal"
        for alert in alerts
    )

    history[-1]["quality"]["median_age_days"] = 30

    alerts = evaluate_alerts(
        {
            "status": "paused",
        },
        history,
        date(2026, 11, 4),
    )

    assert not any(
        alert["kind"] == "early_signal"
        for alert in alerts
    )
