
"""Build evidence-limited reports from completed X Content CSV imports.

Only scalar Firestore fields are stored on post/observation records.
ReportStore stores the resulting structured snapshot as verified JSON.
"""

import json
import re
import statistics

from collections import defaultdict
from datetime import date
from urllib.parse import urlsplit

from app.core.firestore_rest import decode_fields
from app.services.x_content_csv import METRICS

from app.services.observation_identity import (
    UPLOAD_ID_RE,
    conservative_age_days,
    valid_measurement,
)

from app.services.proof_of_work import (
    ProofOfWorkService,
    build_proof,
    month_bounds,
)


_ALLOWED_POST_HOSTS = {
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
}

_POST_PATH = re.compile(
    r"^/([A-Za-z0-9_]{1,15}|i/web)/status/([0-9]{1,128})/?$"
)


def safe_post_url(value, post_id):
    """Accept only HTTPS X post links matching the known Post ID."""

    if not isinstance(value, str) or len(value) > 500:
        return ""

    try:
        parsed = urlsplit(value.strip())
        match = _POST_PATH.fullmatch(parsed.path)

        if (
            parsed.scheme.lower() != "https"
            or parsed.hostname not in _ALLOWED_POST_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or match is None
            or match.group(2) != str(post_id)
        ):
            return ""

        return (
            f"https://x.com/"
            f"{match.group(1)}/status/{match.group(2)}"
        )

    except ValueError:
        return ""


def _number(value):
    return (
        value
        if type(value) is int and value >= 0
        else None
    )


def _stat(values):
    numbers = [
        value
        for value in values
        if value is not None
    ]

    return {
        "n": len(numbers),
        "median": (
            statistics.median(numbers)
            if numbers
            else None
        ),
        "mean": (
            round(statistics.mean(numbers), 1)
            if numbers
            else None
        ),
    }


def _group(rows, field):
    buckets = defaultdict(list)

    for row in rows:
        label = (
            str(row.get(field) or "Unknown")[:80]
            or "Unknown"
        )

        buckets[label].append(row)

    result = []

    for label, group in buckets.items():
        result.append({
            "label": label,
            "count": len(group),
            "median_impressions": _stat(
                [
                    item["impressions"]
                    for item in group
                ]
            )["median"],
            "median_engagements": _stat(
                [
                    item["engagements"]
                    for item in group
                ]
            )["median"],
        })

    return sorted(
        result,
        key=lambda row: (
            -row["count"],
            row["label"].lower(),
        ),
    )[:20]


def _quality(rows, period):
    start, end = month_bounds(period)

    unavailable = {
        "comparable_cohort": False,
        "reason": "",
        "sample_size": len(rows),
        "median_age_days": None,
        "age_quartiles": [],
    }

    if not rows:
        return {
            **unavailable,
            "reason": "No completed-import posts.",
        }

    ages = []
    measurement_keys = set()
    upload_ids = set()
    precisions = set()

    for row in rows:
        observation = row["observation"]

        if not valid_measurement(observation):
            return {
                **unavailable,
                "reason": "Measurement date or precision is missing.",
            }

        try:
            measurement_date = date.fromisoformat(
                observation["measurement_date"]
            )

            published_date = date.fromisoformat(
                row["published_date"]
            )

            full_window = (
                date.fromisoformat(
                    observation["export_start"]
                ) <= start
                and date.fromisoformat(
                    observation["export_end"]
                ) >= end
            )

        except (KeyError, TypeError, ValueError):
            return {
                **unavailable,
                "reason": (
                    "Invalid measurement or export dates."
                ),
            }

        # The CSV has no publication hour; subtract one full day.
        age = conservative_age_days(
            row.get("published_date"),
            observation.get("measurement_date"),
        )

        if (
            not full_window
            or age is None
            or age < 7
            or row["impressions"] is None
        ):
            return {
                **unavailable,
                "reason": (
                    "Incomplete coverage, missing metrics "
                    "or post age below 7 days."
                ),
            }

        ages.append(age)

        measurement_keys.add(
            observation.get("measurement_key")
        )

        upload_ids.add(
            observation.get("upload_id")
        )

        precisions.add(
            observation.get("measurement_precision")
        )

    if (
        len(rows) < 5
        or len(measurement_keys) != 1
        or None in measurement_keys
        or len(upload_ids) != 1
        or not all(
            isinstance(upload_id, str)
            and UPLOAD_ID_RE.fullmatch(upload_id)
            for upload_id in upload_ids
        )
        or len(precisions) != 1
    ):
        return {
            **unavailable,
            "reason": (
                "Mixed completed CSV uploads or snapshots, "
                "or fewer than five posts."
            ),
        }

    quartiles = statistics.quantiles(
        ages,
        n=4,
        method="inclusive",
    )

    return {
        "comparable_cohort": True,
        "reason": (
            "One completed CSV upload and a full-month cohort; "
            "descriptive use only."
        ),
        "sample_size": len(rows),
        "median_age_days": statistics.median(ages),
        "age_quartiles": [
            round(quartiles[0], 1),
            round(quartiles[2], 1),
        ],
    }


class ReportingService:
    """Read records using an owner-scoped TenantStore."""

    def __init__(self, store):
        self.store = store
        self.proof = ProofOfWorkService(store)

    def history(self, client_id, limit=40):
        """Return recent published summaries, never counting drafts as reports.

        Report parents are tenant/client scoped. Scan with Firestore paging,
        then select the latest publication periods; the default document
        order is not assumed to be newest-first.
        """

        self.store.get_client(client_id)

        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError(
                "History limit must be between 1 and 100."
            )

        collection = (
            self.store.client_name(client_id)
            + "/reports"
        )

        token = ""
        visited = set()
        examined = 0
        max_scanned = 5000
        result = []
        scan_truncated = False

        while True:
            response = self.store.list_collection(
                collection,
                page_size=50,
                page_token=token,
            )

            for document in response.get("documents", []):
                examined += 1

                if examined > max_scanned:
                    scan_truncated = True
                    break

                fields = decode_fields(
                    document.get("fields", {})
                )

                if not fields.get("published_revision"):
                    continue

                report_period = fields.get("period", "")

                try:
                    month_bounds(report_period)

                except (TypeError, ValueError):
                    continue

                try:
                    summary = json.loads(
                        fields.get("published_summary_json") or "{}"
                    )

                except (TypeError, ValueError):
                    summary = {}

                if not isinstance(summary, dict):
                    summary = {}

                result.append({
                    "period": report_period,
                    "published_at": fields.get("published_at", ""),
                    "sample_size": summary.get("sample_size"),
                    "median_impressions": summary.get("median_impressions"),
                    "median_engagements": summary.get("median_engagements"),
                    "quality": summary.get("quality", {}),
                })

            if scan_truncated:
                break

            next_token = response.get("nextPageToken", "")

            if not next_token:
                break

            if next_token in visited:
                raise ValueError(
                    "Published history page token repeated."
                )

            visited.add(next_token)
            token = next_token

        ordered = sorted(
            result,
            key=lambda row: row["period"],
        )

        return (
            ordered[-limit:],
            scan_truncated or len(ordered) > limit,
        )

    def build(self, client_id, period, notes):
        """Freeze completed-import evidence into an immutable V2 snapshot.

        Original V1 revisions stay readable. Per-post cumulative metrics
        are never mislabeled as account-wide monthly traffic.
        """

        start, end = month_bounds(period)

        client = self.store.get_client(client_id)

        manual, _ = self.proof.get_manual(
            client_id,
            period,
        )

        source_posts = self.proof._month_posts(
            client_id,
            period,
        )

        proof = build_proof(
            client,
            period,
            source_posts,
            manual,
        )

        enriched = []

        for post in source_posts:
            fields = post["fields"]
            observation = post["observation"]

            item = {
                **post,
                "post_text": str(
                    fields.get("post_text") or ""
                )[:500],
                "post_url": safe_post_url(
                    fields.get("post_url"),
                    post["post_id"],
                ),
                "topic": str(
                    fields.get("topic") or "Unknown"
                )[:80],
                "format": str(
                    fields.get("format") or "Unknown"
                )[:80],
            }

            for metric in METRICS:
                item[metric] = _number(
                    observation.get(metric)
                )

            enriched.append(item)

        metric_names = (
            "impressions",
            "engagements",
        ) + tuple(
            metric
            for metric in METRICS
            if metric not in {
                "impressions",
                "engagements",
            }
        )

        summaries = {}
        total_posts = len(enriched)

        for metric in metric_names:
            values = [
                post[metric]
                for post in enriched
            ]

            present = [
                value
                for value in values
                if value is not None
            ]

            summaries[metric] = {
                **_stat(values),
                "total": (
                    sum(present)
                    if present
                    else None
                ),
                "missing": total_posts - len(present),
                "coverage": f"{len(present)}/{total_posts}",
            }

        group_names = (
            ("Ghostwriter", "Post"),
            ("Ghostwriter", "Reply"),
            ("Client", "Post"),
            ("Client", "Reply"),
        )

        by_type = []
        categorized_count = 0

        for authorship, content_type in group_names:
            members = [
                post
                for post in enriched
                if post.get("authorship") == authorship
                and post.get("content_type") == content_type
            ]

            categorized_count += len(members)

            by_type.append({
                "authorship": authorship,
                "content_type": content_type,
                "count": len(members),
                "impressions": _stat(
                    [
                        post["impressions"]
                        for post in members
                    ]
                ),
                "engagements": _stat(
                    [
                        post["engagements"]
                        for post in members
                    ]
                ),
            })

        uncategorized = total_posts - categorized_count

        per_date = defaultdict(list)

        for post in enriched:
            per_date[
                post["published_date"]
            ].append(post)

        daily_series = []

        for published, posts in sorted(
            per_date.items()
        ):
            known_impressions = [
                post["impressions"]
                for post in posts
                if post["impressions"] is not None
            ]

            known_engagements = [
                post["engagements"]
                for post in posts
                if post["engagements"] is not None
            ]

            daily_series.append({
                "published_date": published,
                "post_count": len(posts),
                "observed_post_impressions": (
                    sum(known_impressions)
                    if known_impressions
                    else None
                ),
                "impressions_coverage": len(
                    known_impressions
                ),
                "observed_post_engagements": (
                    sum(known_engagements)
                    if known_engagements
                    else None
                ),
                "engagements_coverage": len(
                    known_engagements
                ),
            })

        measurement_dates = sorted({
            post["observation"]["measurement_date"]
            for post in enriched
            if isinstance(
                post["observation"].get("measurement_date"),
                str,
            )
        })

        precision_types = sorted({
            post["observation"]["measurement_precision"]
            for post in enriched
            if isinstance(
                post["observation"].get("measurement_precision"),
                str,
            )
        })

        ages = []

        for post in enriched:
            age = conservative_age_days(
                post.get("published_date"),
                post["observation"].get("measurement_date"),
            )

            if age is not None:
                ages.append(age)

        top = sorted(
            (
                post
                for post in enriched
                if post["impressions"] is not None
            ),
            key=lambda post: (
                -post["impressions"],
                post["post_id"],
            ),
        )[:10]

        history, more = self.history(
            client_id
        )

        history = [
            entry
            for entry in history
            if entry["period"] <= period
        ]

        quality = _quality(
            enriched,
            period,
        )

        return {
            "schema_version": 2,
            "period": period,
            "window": {
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            "client": {
                "display_name": client.get(
                    "display_name",
                    "Client",
                )[:100],
                "collaboration_started": client.get(
                    "collaboration_started",
                    "",
                ),
                "x_handle": client.get(
                    "x_handle", ""
                ),
            },
            "brand": notes["brand"],
            "writing": {
                "executive_summary": notes[
                    "executive_summary"
                ],
                "next_month_plan": notes[
                    "next_month_plan"
                ],
                "client_message": notes[
                    "client_message"
                ],
            },
            "observed": {
                "post_count": total_posts,
                "impressions": summaries[
                    "impressions"
                ],
                "engagements": summaries[
                    "engagements"
                ],
                "likes": summaries[
                    "likes"
                ],
                "metrics": summaries,
                "unknown_classifications": proof[
                    "unknown_count"
                ],
                "not_categorized_count": uncategorized,
                "scope": (
                    "Cumulative post-attributed observations from completed "
                    "CSV imports, not monthly account traffic."
                ),
            },
            "breakdown": {
                "authorship_content_type": by_type,
                "not_categorized_count": uncategorized,
            },
            "charts": {
                "daily_posts": daily_series,
                "series_description": (
                    "Posts grouped by publication date; metric values are "
                    "later cumulative observations, not that day's impressions."
                ),
            },
            "measurement": {
                "dates": measurement_dates,
                "precision": precision_types,
                "ages_conservative_days": {
                    "n": len(ages),
                    "min": (
                        min(ages)
                        if ages
                        else None
                    ),
                    "median": (
                        statistics.median(ages)
                        if ages
                        else None
                    ),
                    "max": (
                        max(ages)
                        if ages
                        else None
                    ),
                },
                "published_hour_known": False,
            },
            "proof": proof,
            "topics": _group(
                enriched,
                "topic",
            ),
            "formats": _group(
                enriched,
                "format",
            ),
            "top_posts": [
                {
                    key: post[key]
                    for key in (
                        "post_id",
                        "published_date",
                        "post_text",
                        "post_url",
                        "authorship",
                        "content_type",
                        "topic",
                        "format",
                        "impressions",
                        "engagements",
                        "likes",
                    )
                }
                for post in top
            ],
            "goal": {
                "target_post_impressions": client.get(
                    "post_impressions_goal"
                ),
                "observed_median_post_impressions": summaries[
                    "impressions"
                ]["median"],
                "interpretation": (
                    "Snapshot median, not verified attainment "
                    "of a contractual KPI."
                ),
            },
            "since_started": {
                "collaboration_started": client.get(
                    "collaboration_started"
                ),
                "published_report_history": history,
                "history_truncated": more,
                "note": (
                    "Frozen published revisions through this reporting "
                    "month only, not a complete X account history."
                ),
            },
            "quality": quality,
            "limitations": [
                "Cumulative post observations are not monthly account totals.",
                "Missing X metrics remain missing, not zero.",
                "Classification and manually declared deliverables are not third-party verified.",
                "No causal effect or follower growth is inferred.",
                "Observation ages and export windows may differ between records.",
                "Historical comparisons require aligned cohorts; no automatic growth percentage is asserted.",
            ],
        }
