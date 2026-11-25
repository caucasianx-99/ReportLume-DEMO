"""Conservative, source-aware Proof of Work calculations.

Never convert cumulative per-post X metrics
into monthly account traffic.
"""

import calendar
import re
import statistics

from datetime import date, datetime, timezone

from app.core.firestore_rest import (
    FirestoreConflict,
    FirestoreNotFound,
    FirestoreUnavailable,
    decode_fields,
    encode_fields,
)

from app.services.monthly_data import load_month_posts

from app.services.observation_identity import (
    UPLOAD_ID_RE,
    conservative_age_days,
    valid_measurement,
)

from app.services.client_workspace import (
    WorkspaceReadOnly,
    has_paid_access,
    make_update,
    require_write_access,
)


PERIOD_RE = re.compile(
    r"^\d{4}-(0[1-9]|1[0-2])$"
)

MIN_GROUP = 5
MIN_AGE_DAYS = 7

MAX_MEDIAN_AGE_GAP = 3
MAX_IQR_GAP = 4
MAX_RANGE_GAP = 7


class ProofError(ValueError):
    pass


class ProofConflict(RuntimeError):
    pass


def month_bounds(period: str):
    if (
        not isinstance(period, str)
        or not PERIOD_RE.fullmatch(period)
    ):
        raise ProofError(
            "Reporting period must be YYYY-MM."
        )

    year, month = map(
        int,
        period.split("-"),
    )

    if not 2000 <= year <= 2100:
        raise ProofError(
            "Reporting period is out of range."
        )

    return (
        date(year, month, 1),
        date(
            year,
            month,
            calendar.monthrange(year, month)[1],
        ),
    )


def parse_manual(data):
    result = {}

    for key in (
        "completed_threads",
        "completed_strategy_calls",
    ):
        raw = str(
            data.get(key, "")
        ).strip()

        if raw == "":
            result[key] = None
            continue

        if (
            not raw.isascii()
            or not raw.isdecimal()
            or len(raw) > 6
        ):
            raise ProofError(
                f"{key} must be a non-negative whole number."
            )

        amount = int(raw)

        if amount > 100000:
            raise ProofError(
                f"{key} is too large."
            )

        result[key] = amount

    note = data.get(
        "delivery_notes", ""
    )

    if (
        not isinstance(note, str)
        or len(note) > 1000
    ):
        raise ProofError(
            "Delivery notes must be at most 1000 characters."
        )

    result["delivery_notes"] = note.strip()

    return result


def _age(post):
    """Conservative age from published and measured calendar dates.

    The CSV contains no posting hour. Both date-only and confirmed-minute
    imports receive the same one-day safety adjustment.
    """

    observation = post.get(
        "observation",
        {},
    )

    if not valid_measurement(observation):
        return None

    return conservative_age_days(
        post.get("published_date"),
        observation.get("measurement_date"),
    )


def evaluate_comparison(posts, period):
    """Compare only defensible, aligned cohorts."""

    start, end = month_bounds(period)

    groups = {
        "Ghostwriter": [],
        "Client": [],
    }

    for post in posts:
        if (
            post.get("authorship") in groups
            and post.get("content_type") == "Post"
        ):
            groups[
                post["authorship"]
            ].append(post)

    counts = {
        group: len(rows)
        for group, rows in groups.items()
    }

    result = {
        "status": "not_comparable",
        "reason": "",
        "n": counts,
    }

    if min(counts.values()) < MIN_GROUP:
        result["reason"] = (
            "At least five explicitly labeled Posts "
            "are required in each group."
        )

        return result

    all_posts = (
        groups["Ghostwriter"]
        + groups["Client"]
    )

    upload_ids = set()
    measurement_keys = set()
    precisions = set()
    ages = {}

    for group, rows in groups.items():
        ages[group] = []

        for post in rows:
            obs = post["observation"]

            if any(
                type(obs.get(metric)) is not int
                or obs[metric] < 0
                for metric in (
                    "impressions",
                    "engagements",
                )
            ):
                result["reason"] = (
                    "One or more classified Posts have "
                    "missing comparable metrics."
                )

                return result

            if not valid_measurement(obs):
                result["reason"] = (
                    "Measurement date or precision is missing."
                )

                return result

            try:
                full_coverage = (
                    date.fromisoformat(
                        obs["export_start"]
                    ) <= start
                    and date.fromisoformat(
                        obs["export_end"]
                    ) >= end
                )

            except (
                ValueError,
                KeyError,
                TypeError,
            ):
                full_coverage = False

            if not full_coverage:
                result["reason"] = (
                    "An observation has no confirmed "
                    "full-month export window."
                )

                return result

            age = _age(post)

            if (
                age is None
                or age < MIN_AGE_DAYS
            ):
                result["reason"] = (
                    "Posts must have at least seven "
                    "conservative observed days and a known "
                    "measurement date."
                )

                return result

            ages[group].append(age)

            upload_ids.add(
                obs.get("upload_id")
            )

            measurement_keys.add(
                obs.get("measurement_key")
            )

            precisions.add(
                obs.get("measurement_precision")
            )

    # Two different files can have the same measurement date and key.
    # Comparison identity must therefore be the same completed upload.
    if (
        len(upload_ids) != 1
        or not all(
            isinstance(upload_id, str)
            and UPLOAD_ID_RE.fullmatch(upload_id)
            for upload_id in upload_ids
        )
        or len(measurement_keys) != 1
        or None in measurement_keys
        or len(precisions) != 1
    ):
        result["reason"] = (
            "The two groups must come from the same "
            "completed CSV upload and measurement snapshot."
        )

        return result

    left = sorted(
        ages["Ghostwriter"]
    )

    right = sorted(
        ages["Client"]
    )

    if abs(
        statistics.median(left)
        - statistics.median(right)
    ) > MAX_MEDIAN_AGE_GAP:
        result["reason"] = (
            "Median post observation ages "
            "differ too much."
        )

        return result

    left_q = statistics.quantiles(
        left,
        n=4,
        method="inclusive",
    )

    right_q = statistics.quantiles(
        right,
        n=4,
        method="inclusive",
    )

    if any(
        abs(left_q[i] - right_q[i])
        > MAX_IQR_GAP
        for i in (0, 2)
    ):
        result["reason"] = (
            "Post age quartiles "
            "are not comparable."
        )

        return result

    left_range = max(left) - min(left)
    right_range = max(right) - min(right)

    if abs(
        left_range - right_range
    ) > MAX_RANGE_GAP:
        result["reason"] = (
            "Post age ranges are not comparable."
        )

        return result

    result = {
        "status": "descriptive_only",
        "reason": (
            "Same completed CSV measurement snapshot; "
            "descriptive association, not causation."
        ),
        "n": counts,
        "measurement_date": (
            all_posts[0]["observation"]
            .get("measurement_date")
        ),
        "groups": {},
    }

    for group, rows in groups.items():
        impressions = [
            post["observation"]["impressions"]
            for post in rows
        ]

        engagements = [
            post["observation"]["engagements"]
            for post in rows
        ]

        result["groups"][group] = {
            "n": len(rows),
            "median_impressions": (
                statistics.median(impressions)
            ),
            "mean_impressions": round(
                statistics.mean(impressions),
                1,
            ),
            "median_engagements": (
                statistics.median(engagements)
            ),
            "mean_engagements": round(
                statistics.mean(engagements),
                1,
            ),
            "median_age_days": (
                statistics.median(
                    ages[group]
                )
            ),
        }

    return result


def build_proof(client, period, posts, manual):
    month_bounds(period)

    ghost_posts = sum(
        post.get("authorship") == "Ghostwriter"
        and post.get("content_type") == "Post"
        for post in posts
    )

    ghost_replies = sum(
        post.get("authorship") == "Ghostwriter"
        and post.get("content_type") == "Reply"
        for post in posts
    )

    unknown = sum(
        post.get("authorship", "Unknown") == "Unknown"
        or post.get("content_type", "Unknown") == "Unknown"
        for post in posts
    )

    present = bool(posts)

    return {
        "period": period,
        "eligible_post_count": len(posts),
        "unknown_count": unknown,
        "manual": manual,
        "deliverables": [
            {
                "name": "Posts",
                "committed": client.get(
                    "monthly_posts", 0
                ),
                "delivered": (
                    ghost_posts if present else None
                ),
                "source": (
                    "computed (x_analytics + ghostwriter_input)"
                ),
            },
            {
                "name": "Replies",
                "committed": client.get(
                    "monthly_replies", 0
                ),
                "delivered": (
                    ghost_replies if present else None
                ),
                "source": (
                    "computed (x_analytics + ghostwriter_input)"
                ),
            },
            {
                "name": "Threads",
                "committed": client.get(
                    "monthly_threads", 0
                ),
                "delivered": manual.get(
                    "completed_threads"
                ),
                "source": "ghostwriter_input",
            },
            {
                "name": "Strategy calls",
                "committed": client.get(
                    "monthly_strategy_calls", 0
                ),
                "delivered": manual.get(
                    "completed_strategy_calls"
                ),
                "source": "ghostwriter_input",
            },
        ],
        "comparison": evaluate_comparison(
            posts,
            period,
        ),
    }


class ProofOfWorkService:

    def __init__(self, store):
        self.store = store

    def _period_name(self, client_id, period):
        month_bounds(period)

        return (
            self.store.client_name(client_id)
            + "/periods/"
            + period
        )

    def get_manual(self, client_id, period):
        name = self._period_name(
            client_id,
            period,
        )

        try:
            document = self.store.get_document(name)

        except FirestoreNotFound:
            return {
                "completed_threads": None,
                "completed_strategy_calls": None,
                "delivery_notes": "",
            }, ""

        fields = decode_fields(
            document.get("fields", {})
        )

        return {
            "completed_threads": fields.get(
                "completed_threads"
            ),
            "completed_strategy_calls": fields.get(
                "completed_strategy_calls"
            ),
            "delivery_notes": fields.get(
                "delivery_notes", ""
            ),
        }, document["updateTime"]

    def save_manual(
        self,
        client_id,
        period,
        raw,
        expected_version,
    ):
        month_bounds(period)

        values = parse_manual(raw)

        profile, _ = (
            self.store.get_user_with_version()
        )

        require_write_access(profile)

        client = self.store.get_client(
            client_id
        )

        if client.get("status") != "active":
            raise WorkspaceReadOnly(
                "Only active clients can be edited."
            )

        if (
            not has_paid_access(profile)
            and profile.get("unpaid_client_id")
            != client_id
        ):
            raise WorkspaceReadOnly(
                "This client is not covered "
                "by your current access."
            )

        if (
            not isinstance(expected_version, str)
            or len(expected_version) > 80
        ):
            raise ProofError(
                "Invalid document version."
            )

        name = self._period_name(
            client_id,
            period,
        )

        fields = {
            **values,
            "owner_uid": self.store.uid,
            "source": "ghostwriter_input",
            "updated_at": datetime.now(
                timezone.utc
            ).isoformat(),
        }

        if expected_version:
            write = make_update(
                name,
                fields,
                expected_version,
            )

        else:
            write = {
                "update": {
                    "name": name,
                    "fields": encode_fields(fields),
                },
                "currentDocument": {
                    "exists": False,
                },
            }

        try:
            self.store.commit([write])

        except FirestoreConflict as exc:
            raise ProofConflict(
                "Another change was saved. "
                "Refresh before editing again."
            ) from exc

    def _month_posts(self, client_id, period):
        """Load one month with stable pagination and batched observations.

        Returns the same classification and observation keys as the former
        reader, with source post fields attached for report generation.
        """

        start, end = month_bounds(period)

        return load_month_posts(
            self.store,
            client_id,
            start,
            end,
        )

    def overview(self, client_id, period):
        self.store.get_client(client_id)

        manual, version = self.get_manual(
            client_id,
            period,
        )

        client = self.store.get_client(
            client_id
        )

        posts = self._month_posts(
            client_id,
            period,
        )

        return (
            build_proof(
                client,
                period,
                posts,
                manual,
            ),
            version,
            client,
        )
