"""Proof of Work unit tests with synthetic observations."""

from datetime import date, timedelta

import pytest

from app.core.firestore_rest import (
    FirestoreConflict,
    FirestoreNotFound,
)

from app.services.proof_of_work import (
    ProofConflict,
    ProofError,
    ProofOfWorkService,
    build_proof,
    evaluate_comparison,
    month_bounds,
    parse_manual,
)


PERIOD = "2026-09"

OBSERVATION = {
    "upload_id": "b" * 64,
    "measurement_precision": "confirmed_minute",
    "measurement_at": "2026-10-15T10:00:00+00:00",
    "measurement_date": "2026-10-15",
    "measurement_key": (
        "minute:2026-10-15T10:00+00:00"
    ),
    "export_start": "2026-09-01",
    "export_end": "2026-09-30",
    "impressions": 100,
    "engagements": 12,
}


def cohort():
    records = []

    for author in (
        "Ghostwriter",
        "Client",
    ):
        for index in range(5):
            records.append({
                "authorship": author,
                "content_type": "Post",
                "published_date": (
                    date(2026, 9, 5)
                    + timedelta(days=index)
                ).isoformat(),
                "observation": {
                    **OBSERVATION,
                    "impressions": 100 + index,
                },
            })

    return records


def test_five_plus_five_descriptive_comparison():
    result = evaluate_comparison(
        cohort(),
        PERIOD,
    )

    assert result["status"] == "descriptive_only"

    assert result["n"] == {
        "Ghostwriter": 5,
        "Client": 5,
    }

    assert (
        result["groups"]["Ghostwriter"][
            "median_impressions"
        ]
        == 102
    )


def test_insufficient_client_sample():
    posts = cohort()[:-1]

    result = evaluate_comparison(
        posts,
        PERIOD,
    )

    assert result["status"] == "not_comparable"


def test_different_measurement_snapshots():
    posts = cohort()

    posts[0]["observation"][
        "measurement_key"
    ] = "another-snapshot"

    assert evaluate_comparison(
        posts,
        PERIOD,
    )["status"] == "not_comparable"


def test_different_age_distributions():
    posts = cohort()

    for post in posts[5:]:
        post["published_date"] = "2026-09-28"

    assert evaluate_comparison(
        posts,
        PERIOD,
    )["status"] == "not_comparable"


def test_inconsistent_precision_within_one_upload():
    posts = cohort()

    posts[0]["observation"][
        "measurement_precision"
    ] = "date_only_time_unknown"

    assert evaluate_comparison(
        posts,
        PERIOD,
    )["status"] == "not_comparable"


def test_date_only_measurement_is_comparable_without_clock_time():
    posts = cohort()

    for post in posts:
        post["observation"]["measurement_precision"] = (
            "date_only_time_unknown"
        )

        post["observation"]["measurement_at"] = None

        post["observation"]["measurement_key"] = (
            "date_only:2026-10-15:Asia/Baku"
        )

    result = evaluate_comparison(
        posts,
        PERIOD,
    )

    assert result["status"] == "descriptive_only"

    assert result["n"] == {
        "Ghostwriter": 5,
        "Client": 5,
    }


def test_two_files_with_same_date_are_never_a_comparison_cohort():
    posts = cohort()

    for post in posts[5:]:
        post["observation"]["upload_id"] = "c" * 64

    result = evaluate_comparison(
        posts,
        PERIOD,
    )

    assert result["status"] == "not_comparable"

    assert "same completed CSV" in result["reason"]


def test_missing_metric_not_treated_as_zero():
    posts = cohort()

    posts[0]["observation"][
        "impressions"
    ] = None

    assert evaluate_comparison(
        posts,
        PERIOD,
    )["status"] == "not_comparable"


def test_partial_export_window():
    posts = cohort()

    posts[0]["observation"][
        "export_start"
    ] = "2026-09-12"

    assert evaluate_comparison(
        posts,
        PERIOD,
    )["status"] == "not_comparable"


def test_missing_manual_value_stays_missing():
    result = parse_manual({
        "completed_threads": "",
        "completed_strategy_calls": "0",
        "delivery_notes": "Confirmed work",
    })

    assert result["completed_threads"] is None
    assert result["completed_strategy_calls"] == 0


def test_negative_manual_delivery_rejected():
    with pytest.raises(ProofError):
        parse_manual({
            "completed_threads": "-3",
            "completed_strategy_calls": "0",
        })


def test_unknown_author_not_credited():
    posts = cohort()

    posts.append({
        "authorship": "Unknown",
        "content_type": "Post",
        "observation": {
            **OBSERVATION,
        },
    })

    proof = build_proof(
        {
            "monthly_posts": 6,
        },
        PERIOD,
        posts,
        {},
    )

    assert proof["deliverables"][0][
        "delivered"
    ] == 5

    assert proof["unknown_count"] == 1


def test_no_posts_is_not_zero_delivery():
    proof = build_proof(
        {},
        PERIOD,
        [],
        {},
    )

    assert proof["deliverables"][0][
        "delivered"
    ] is None


def test_invalid_period_rejected():
    with pytest.raises(ProofError):
        month_bounds("2026-13")


class FakeStore:
    uid = "verified_user"

    def __init__(self):
        self.writes = []
        self.existing = False

    def client_name(self, client_id):
        return (
            "projects/test/databases/(default)"
            "/documents/users/verified_user"
            "/clients/"
            + client_id
        )

    def get_user_with_version(self):
        return {
            "unpaid_client_id": "client",
        }, "version"

    def get_client(self, client_id):
        return {
            "status": "active",
        }

    def get_document(self, name):
        if not self.existing:
            raise FirestoreNotFound(
                "Document missing"
            )

        return {
            "updateTime": "new-version",
            "fields": {},
        }

    def commit(self, writes):
        self.writes.append(writes)

        if self.existing:
            raise FirestoreConflict(
                "Conflict"
            )

        return {
            "commitTime": "synthetic-test",
        }


def test_new_manual_record_has_create_precondition():
    store = FakeStore()

    service = ProofOfWorkService(
        store
    )

    service.save_manual(
        "client",
        PERIOD,
        {
            "completed_threads": "2",
            "completed_strategy_calls": "0",
            "delivery_notes": "Work completed",
        },
        "",
    )

    assert store.writes[0][0][
        "currentDocument"
    ] == {"exists": False}


def test_concurrent_creation_rejected():
    store = FakeStore()
    store.existing = True

    service = ProofOfWorkService(
        store
    )

    with pytest.raises(ProofConflict):
        service.save_manual(
            "client",
            PERIOD,
            {
                "completed_threads": "2",
                "completed_strategy_calls": "0",
                "delivery_notes": "",
            },
            "",
        )
