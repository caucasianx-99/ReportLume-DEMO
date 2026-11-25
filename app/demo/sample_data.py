"""Deterministic, entirely fictional client-reporting demonstration.

This module has no database, network, uploads or filesystem writes.
The snapshot follows the same V2 shape as a frozen customer report.
"""

from collections import Counter, defaultdict
from datetime import date
from statistics import median

from app.services.x_content_csv import ENGAGEMENT_PARTS, METRICS


CLIENT_NAME = "Northstar Studio (fictional)"
AGENCY_NAME = "Aster Creative (fictional)"
MEASUREMENT_DATE = "2026-11-04"

PUBLISHING_DAYS = (
    1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14,
    15, 16, 17, 19, 20, 21, 22, 23, 24,
)

POST_ID_BASE = 10**18

TOPICS = (
    "Product education",
    "Onboarding",
    "Customer questions",
)

FORMATS = (
    "Educational",
    "Opinion",
    "Personal Story",
)

POST_IDEAS = (
    "A practical product lesson from this week's customer questions",
    "Three onboarding mistakes worth preventing before launch",
    "How to explain a feature without overwhelming a new customer",
    "A checklist for making product updates easier to understand",
    "What a small customer experiment can teach a product team",
    "A founder's perspective on writing for a defined audience",
    "Ways to turn support questions into useful educational content",
    "Why clear documentation can help customers work independently",
    "The one question we ask before shipping any new feature",
    "What 50 support tickets taught us about our pricing page",
    "How we cut onboarding from two weeks to three days",
    "Five product decisions we would make differently today",
    "Why our best customers found us through a single blog post",
)

REPLY_IDEAS = (
    "@example_reader A useful place to start is with one customer problem.",
    "@example_reader That distinction is helpful. We'd measure both separately.",
    "@example_reader Good question. The checklist is meant for small teams too.",
    "@example_reader I'd first test the shorter explanation with new users.",
)


def _sample_posts():
    """72 exported rows; exactly 70 belong to October 2026."""
    rows = []

    for index in range(72):
        if index == 0:
            published = "2026-09-29"
            author, kind = "Client", "Post"

        elif index == 71:
            published = "2026-11-02"
            author, kind = "Client", "Post"

        else:
            position = index - 1
            day = PUBLISHING_DAYS[
                (position * 11 + position // 5)
                % len(PUBLISHING_DAYS)
            ]

            published = f"2026-10-{day:02d}"

            if position < 26:
                author, kind = "Ghostwriter", "Post"

            elif position < 58:
                author, kind = "Ghostwriter", "Reply"

            elif position < 66:
                author, kind = "Client", "Post"

            else:
                author, kind = "Client", "Reply"

        position = max(0, index - 1)

        spread = position * 73 + position // 4 * 11

        text = (
            REPLY_IDEAS[position % len(REPLY_IDEAS)]
            if kind == "Reply"
            else POST_IDEAS[(spread // 7) % len(POST_IDEAS)]
        )

        # Replies usually reach fewer people than original posts.
        impressions = (
            120 + spread % 380
            if kind == "Reply"
            else 520 + spread % 1480
        )

        components = {
            "likes": 3 + impressions // 45 + position % 5,
            "replies": position % 4,
            "reposts": position % 5,
            "bookmarks": position % 4,
            "shares": position % 3,
            "new_follows": position % 2,
            "profile_visits": position % 8,
            "detail_expands": position % 10,
            "url_clicks": position % 5,
            "hashtag_clicks": position % 2,
            "permalink_clicks": position % 6,
        }

        engagements = sum(
            components[key]
            for key in ENGAGEMENT_PARTS
        )

        rows.append({
            "post_id": str(POST_ID_BASE + index),
            "published_date": published,
            "post_text": text,
            "post_url": "",
            "authorship": author,
            "content_type": kind,
            "topic": TOPICS[position % len(TOPICS)],
            "format": FORMATS[
                (position // 2) % len(FORMATS)
            ],
            "impressions": impressions,
            "engagements": engagements,
            **components,
        })

    return rows


def _metric_summary(rows, key):
    values = [
        row[key]
        for row in rows
        if type(row.get(key)) is int
    ]

    return {
        "n": len(values),
        "median": (
            median(values)
            if values
            else None
        ),
        "mean": (
            round(sum(values) / len(values), 1)
            if values
            else None
        ),
        "total": (
            sum(values)
            if values
            else None
        ),
        "missing": len(rows) - len(values),
        "coverage": f"{len(values)}/{len(rows)}",
    }


def _breakdown(rows):
    result = []

    for author in ("Ghostwriter", "Client"):
        for kind in ("Post", "Reply"):
            group = [
                row
                for row in rows
                if (
                    row["authorship"] == author
                    and row["content_type"] == kind
                )
            ]

            result.append({
                "authorship": author,
                "content_type": kind,
                "count": len(group),
                "impressions": _metric_summary(
                    group,
                    "impressions",
                ),
                "engagements": _metric_summary(
                    group,
                    "engagements",
                ),
            })

    return result


def _categories(rows, key):
    groups = defaultdict(list)

    for row in rows:
        groups[row[key]].append(row)

    return [
        {
            "label": label,
            "count": len(group),
            "median_impressions": median(
                row["impressions"]
                for row in group
            ),
            "median_engagements": median(
                row["engagements"]
                for row in group
            ),
        }
        for label, group in sorted(groups.items())
    ]


def build_sample():
    all_posts = _sample_posts()

    rows = [
        row
        for row in all_posts
        if row["published_date"].startswith("2026-10-")
    ]

    metrics = {
        key: _metric_summary(rows, key)
        for key in METRICS
    }

    counts = Counter(
        row["published_date"]
        for row in rows
    )

    daily = [
        {
            "published_date": day,
            "post_count": count,
            "observed_post_impressions": sum(
                row["impressions"]
                for row in rows
                if row["published_date"] == day
            ),
            "impressions_coverage": count,
            "observed_post_engagements": sum(
                row["engagements"]
                for row in rows
                if row["published_date"] == day
            ),
            "engagements_coverage": count,
        }
        for day, count in sorted(counts.items())
    ]

    ages = [
        max(
            0,
            (
                date.fromisoformat(MEASUREMENT_DATE)
                - date.fromisoformat(
                    row["published_date"]
                )
            ).days - 1,
        )
        for row in rows
    ]

    groups = _breakdown(rows)

    ghost_posts = next(
        group["count"]
        for group in groups
        if (
            group["authorship"] == "Ghostwriter"
            and group["content_type"] == "Post"
        )
    )

    ghost_replies = next(
        group["count"]
        for group in groups
        if (
            group["authorship"] == "Ghostwriter"
            and group["content_type"] == "Reply"
        )
    )

    # Same order as "top_posts" below: most impressions, then lowest id.
    top_post = min(
        rows,
        key=lambda row: (-row["impressions"], row["post_id"]),
    )

    best_format = max(
        _categories(rows, "format"),
        key=lambda item: item["median_impressions"],
    )["label"]

    top_topic = max(
        _categories(rows, "topic"),
        key=lambda item: item["count"],
    )["label"]

    snapshot = {
        "schema_version": 2,
        "period": "2026-10",
        "window": {
            "start": "2026-10-01",
            "end": "2026-10-31",
        },
        "client": {
            "display_name": CLIENT_NAME,
            "collaboration_started": "2026-09-15",
            "x_handle": "",
        },
        "brand": AGENCY_NAME,
        "brand_theme": "teal",
        "brand_logo_b64": "",
        "trial_watermark": False,
        "writing": {
            "executive_summary": (
                "In October we delivered everything planned "
                f"for the month: {ghost_posts} original posts, "
                f"{ghost_replies} replies, 2 threads and a "
                "strategy call. The strongest post was "
                f"“{top_post['post_text']}” with "
                f"{top_post['impressions']:,} impressions. "
                f"{top_topic} was the most frequent theme, and "
                "the typical post cleared the per-post "
                "impression target set for the month."
            ),
            "next_month_plan": (
                "Continue the onboarding series, test two new "
                "customer-question topics and publish more "
                f"{best_format.lower()} posts, the format with "
                "the highest typical reach this month."
            ),
            "client_message": (
                "Thanks for a strong October. Every planned "
                "deliverable shipped. Let's pick November's two "
                "new topics on our next call."
            ),
        },
        "observed": {
            "post_count": len(rows),
            "impressions": metrics["impressions"],
            "engagements": metrics["engagements"],
            "likes": metrics["likes"],
            "metrics": metrics,
            "unknown_classifications": 0,
            "not_categorized_count": 0,
            "scope": (
                "Entirely fictional cumulative post metrics; "
                "not account traffic."
            ),
        },
        "breakdown": {
            "authorship_content_type": groups,
            "not_categorized_count": 0,
        },
        "charts": {
            "daily_posts": daily,
            "series_description": (
                "Fictional posts and replies by publishing date."
            ),
        },
        "measurement": {
            "dates": [MEASUREMENT_DATE],
            "precision": ["date_only_time_unknown"],
            "published_hour_known": False,
            "ages_conservative_days": {
                "n": len(ages),
                "min": min(ages),
                "median": median(ages),
                "max": max(ages),
            },
        },
        "proof": {
            "deliverables": [
                {
                    "name": "Posts",
                    "committed": ghost_posts,
                    "delivered": ghost_posts,
                },
                {
                    "name": "Replies",
                    "committed": ghost_replies,
                    "delivered": ghost_replies,
                },
                {
                    "name": "Threads",
                    "committed": 2,
                    "delivered": 2,
                },
                {
                    "name": "Strategy calls",
                    "committed": 1,
                    "delivered": 1,
                },
            ],
            "comparison": {
                "status": "not_comparable",
                "reason": (
                    "The fictional post groups do not establish "
                    "matched cohorts."
                ),
            },
        },
        "topics": _categories(rows, "topic"),
        "formats": _categories(rows, "format"),
        "top_posts": sorted(
            rows,
            key=lambda row: (
                -row["impressions"],
                row["post_id"],
            ),
        )[:6],
        "goal": {
            "target_post_impressions": 450,
            "observed_median_post_impressions": (
                metrics["impressions"]["median"]
            ),
        },
        "since_started": {
            "collaboration_started": "2026-09-15",
            "published_report_history": [],
            "history_truncated": False,
        },
        "quality": {
            "comparable_cohort": False,
            "reason": (
                "Fictional demonstration; "
                "no causal or account-wide claim."
            ),
        },
        "limitations": [
            "Entirely fictional sample. "
            "No real client or X account data.",
        ],
    }

    return {
        "client_name": CLIENT_NAME,
        "period": "October 2026",
        "csv_record_count": len(all_posts),
        "report_record_count": len(rows),
        "possible_reply_count": sum(
            row["post_text"].startswith("@")
            for row in all_posts
        ),
        "snapshot": snapshot,
        "sample_rows": all_posts[:5],
    }


SAMPLE = build_sample()
