"""Offline checks for reporting, sharing, trial and case consent."""

import copy
import datetime
import hashlib
import json

import pytest

from app.core.firestore_rest import (
    FirestoreConflict,
    FirestoreNotFound,
    encode_fields,
    decode_fields,
)
from app.services.alerts import evaluate_alerts
from app.services.report_store import (
    ReportStore,
    ReportValidation,
)
from app.services.share_case import SharingService, CaseService


class FakeStore:
    uid = "test-owner"
    user_name = (
        "projects/test/databases/(default)/"
        "documents/users/test-owner"
    )

    def __init__(self):
        self.docs = {}
        self.number = 1

        self._seed(
            self.user_name,
            {
                "unpaid_client_id": "client-one",
                "unpaid_client_created": True,
                "trial_started_at": "2026-11-01T00:00:00+00:00",
                "trial_ends_at": "2099-01-01T00:00:00+00:00",
            },
        )

        self._seed(
            self.client_name("client-one"),
            {
                "display_name": "Example Client",
                "status": "active",
                "collaboration_started": "2026-09-01",
            },
        )

    def _seed(self, name, values):
        self.docs[name] = {
            "name": name,
            "fields": encode_fields(values),
            "updateTime": (
                f"2030-01-01T00:00:"
                f"{self.number:02d}.000000Z"
            ),
        }
        self.number += 1

    def client_name(self, client_id):
        return self.user_name + "/clients/" + client_id

    def get_client(self, client_id):
        document = self.get_document(
            self.client_name(client_id)
        )

        return {
            "id": client_id,
            **decode_fields(document["fields"]),
            "_update_time": document["updateTime"],
        }

    def get_user_with_version(self):
        document = self.get_document(self.user_name)

        return (
            decode_fields(document["fields"]),
            document["updateTime"],
        )

    def get_document(self, name):
        if name not in self.docs:
            raise FirestoreNotFound("Not found")

        return copy.deepcopy(self.docs[name])

    def list_collection(
        self,
        resource,
        page_size=20,
        page_token="",
        order_by="",
    ):
        result = []

        for name, document in self.docs.items():
            if (
                name.startswith(resource + "/")
                and len(name.split("/"))
                == len(resource.split("/")) + 1
            ):
                result.append(copy.deepcopy(document))

        return {
            "documents": result[:page_size],
            "nextPageToken": (
                ""
                if len(result) <= page_size
                else "token"
            ),
        }

    def commit(self, writes):
        old = copy.deepcopy(self.docs)

        for write in writes:
            update = write["update"]
            name = update["name"]
            precondition = write.get("currentDocument", {})

            if (
                precondition.get("exists") is False
                and name in old
            ):
                raise FirestoreConflict("Already exists")

            if (
                precondition.get("updateTime")
                and (
                    name not in old
                    or old[name]["updateTime"]
                    != precondition["updateTime"]
                )
            ):
                raise FirestoreConflict("Version mismatch")

        for write in writes:
            update = write["update"]
            name = update["name"]

            fields = copy.deepcopy(
                old.get(name, {}).get("fields", {})
            )

            if "updateMask" in write:
                fields.update(update["fields"])
            else:
                fields = update["fields"]

            self.docs[name] = {
                "name": name,
                "fields": fields,
                "updateTime": (
                    f"2030-01-01T00:00:"
                    f"{self.number:02d}.000000Z"
                ),
            }

            self.number += 1

        return {
            "writeResults": [{} for _ in writes],
        }


SNAPSHOT = {
    "schema_version": 1,
    "period": "2026-10",
    "client": {
        "display_name": "Example Client",
        "collaboration_started": "2026-09-01",
    },
    "brand": "My Agency",
    "observed": {
        "post_count": 6,
        "impressions": {
            "n": 6,
            "median": 110,
            "mean": 100,
        },
        "engagements": {
            "n": 6,
            "median": 7,
            "mean": 7,
        },
    },
    "proof": {
        "comparison": {
            "status": "not_comparable",
            "reason": "Too few groups",
        },
        "deliverables": [],
    },
    "quality": {
        "comparable_cohort": False,
        "reason": "Incomplete",
    },
}


def seed_report(store, period="2026-10"):
    reports = ReportStore(store)
    revision = "a" * 32

    raw = json.dumps(
        {
            **SNAPSHOT,
            "period": period,
        },
        sort_keys=True,
    )

    store._seed(
        reports._name("client-one", period),
        {
            "period": period,
            "status": "published",
            "latest_revision": revision,
            "published_revision": revision,
            "published_at": "2026-11-01T12:00:00Z",
            "published_summary_json": json.dumps({
                "sample_size": 6,
                "median_impressions": 110,
                "quality": {
                    "comparable_cohort": False,
                },
            }),
        },
    )

    store._seed(
        reports._revision_name(
            "client-one",
            period,
            revision,
        ),
        {
            "snapshot_json": raw,
            "sha256": hashlib.sha256(
                raw.encode()
            ).hexdigest(),
            "period": period,
            "revision_id": revision,
        },
    )

    return revision


def test_notes_limit_rejected():
    from app.services.report_store import check_notes

    with pytest.raises(ReportValidation):
        check_notes({
            "executive_summary": "x" * 2001,
        })


def test_source_workspace_has_no_trial_quota():
    from app.services.workspace_access import plan_entitlements, can_publish_unmarked_copy
    from app.core.config import settings
    rights = plan_entitlements({}, settings)
    assert rights["active"] and not rights["trial"]
    assert rights["client_limit"] == 200
    assert can_publish_unmarked_copy({}, settings)


def test_shares_are_revocable_and_pinned():
    store = FakeStore()
    first = seed_report(store)
    sharing = SharingService(store)

    token = sharing.create_link(
        "client-one",
        "2026-10",
        "reviewer",
    )

    assert len(token) >= 40

    link, snapshot, _ = sharing.verify(
        "client-one",
        token,
    )

    assert link["revision_id"] == first
    assert snapshot["period"] == "2026-10"

    sharing.submit(
        "client-one",
        token,
        {
            "reviewer_name": "Reviewer",
            "action": "approve",
            "confirm_approval": "on",
            "comment": "Looks accurate",
        },
    )

    reviews, _ = sharing.feedback(
        "client-one",
        "2026-10",
        first,
    )

    assert reviews[0]["marketing_consent"] is False
    assert (
        reviews[0]["identity_status"]
        == "unverified_link_holder"
    )

    sharing.revoke(
        "client-one",
        hashlib.sha256(
            token.encode()
        ).hexdigest(),
    )

    with pytest.raises(FirestoreNotFound):
        sharing.verify(
            "client-one",
            token,
        )


def test_viewer_cannot_submit_feedback():
    store = FakeStore()
    seed_report(store)

    sharing = SharingService(store)

    token = sharing.create_link(
        "client-one",
        "2026-10",
        "viewer",
    )

    with pytest.raises(
        Exception,
        match="does not allow feedback",
    ):
        sharing.submit(
            "client-one",
            token,
            {
                "action": "comment",
            },
        )


def test_specific_case_consent_required_and_withdrawable():
    store = FakeStore()
    seed_report(store)

    cases = CaseService(store)

    data = {
        "headline": "Writing consistency in a content program",
        "context": (
            "An anonymized content workflow was established."
        ),
        "approach": (
            "Content planning and routine delivery were used."
        ),
        "outcome": (
            "A reporting process captured observed post metrics."
        ),
    }

    cases.save(
        "client-one",
        "2026-10",
        data,
        "",
    )

    with pytest.raises(
        Exception,
        match="consent",
    ):
        cases.publish(
            "client-one",
            "2026-10",
        )

    token = cases.make_consent_link(
        "client-one",
        "2026-10",
    )

    cases.consent(
        "client-one",
        "2026-10",
        token,
        {
            "action": "consent",
            "authorized": "on",
            "case_consent": "on",
            "consent_actor": "Authorized Client Contact",
        },
    )

    cases.publish(
        "client-one",
        "2026-10",
    )

    assert (
        cases.public(
            "client-one",
            "2026-10",
        )["headline"]
        == data["headline"]
    )

    cases.consent(
        "client-one",
        "2026-10",
        token,
        {
            "action": "withdraw",
        },
    )

    with pytest.raises(FirestoreNotFound):
        cases.public(
            "client-one",
            "2026-10",
        )


def test_attention_avoids_unverified_comparisons():
    history = [
        {
            "period": "2026-09",
            "median_impressions": 1000,
            "quality": {
                "comparable_cohort": False,
            },
        },
        {
            "period": "2026-10",
            "median_impressions": 100,
            "quality": {
                "comparable_cohort": False,
            },
        },
    ]

    assert evaluate_alerts(
        {},
        history,
        datetime.date(2026, 11, 3),
    ) == []


def test_attention_renewal_is_date_scoped():
    client = {
        "renewal_date": "2026-11-08",
        "status": "active",
    }

    result = evaluate_alerts(
        client,
        [],
        datetime.date(2026, 11, 3),
    )

    assert len(result) == 1
    assert result[0]["kind"] == "renewal"
