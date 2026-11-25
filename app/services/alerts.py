
"""Conservative owner-only renewal and observed-performance alerts.

An early signal needs two comparable consecutive months.

The stronger performance warning keeps its previous
three-baseline/two-low-month rule.

Neither is a causal attribution or a churn prediction.
"""

import statistics

from datetime import date, timedelta

from app.services.reporting import ReportingService


DECLINE_FRACTION = 0.20

MIN_SAMPLE = 5

MAX_AGE_MEDIAN_GAP = 3
MAX_AGE_QUARTILE_GAP = 4


def _next_month(month):
    year, number = map(
        int,
        month.split("-"),
    )

    return (
        f"{year + (number == 12):04d}-"
        f"{number % 12 + 1:02d}"
    )


def _previous_month(today):
    return (
        today.replace(day=1)
        - timedelta(days=1)
    ).strftime("%Y-%m")


def _aligned(rows):
    """All rows must be reliable, contiguous, and age-aligned."""

    if len(rows) < 2:
        return False

    try:
        for a, b in zip(
            rows,
            rows[1:],
        ):
            if _next_month(
                a["period"]
            ) != b["period"]:
                return False

        qualities = [
            row["quality"]
            for row in rows
        ]

        if any(
            not quality.get("comparable_cohort")
            for quality in qualities
        ):
            return False

        if any(
            type(quality.get("sample_size")) is not int
            or quality["sample_size"] < MIN_SAMPLE
            for quality in qualities
        ):
            return False

        age_medians = [
            quality["median_age_days"]
            for quality in qualities
        ]

        if any(
            type(value) not in (int, float)
            for value in age_medians
        ):
            return False

        if (
            max(age_medians) - min(age_medians)
            > MAX_AGE_MEDIAN_GAP
        ):
            return False

        quartiles = [
            quality["age_quartiles"]
            for quality in qualities
        ]

        if not all(
            isinstance(quartile, list)
            and len(quartile) == 2
            for quartile in quartiles
        ):
            return False

        for index in (0, 1):
            values = [
                quartile[index]
                for quartile in quartiles
            ]

            if any(
                type(value) not in (int, float)
                for value in values
            ):
                return False

            if (
                max(values) - min(values)
                > MAX_AGE_QUARTILE_GAP
            ):
                return False

        scores = [
            row["median_impressions"]
            for row in rows
        ]

        return all(
            type(value) in (int, float)
            and value >= 0
            for value in scores
        )

    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
    ):
        return False


def _human_date(value):
    """Use US-English display conventions for calendar dates."""
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def _human_month(month):
    """Turn an internal YYYY-MM period into a readable month label."""
    try:
        year, number = (
            int(part)
            for part in month.split("-")
        )

        return date(year, number, 1).strftime("%B %Y")

    except (ValueError, TypeError, AttributeError):
        return "the previous month"


def _duration_days(days):
    return f"{days} day" if days == 1 else f"{days} days"


def evaluate_alerts(
    client,
    history,
    today=None,
):
    today = today or date.today()

    alerts = []

    try:
        renewal = date.fromisoformat(
            client.get("renewal_date")
        )

    except (ValueError, TypeError):
        renewal = None

    if (
        renewal
        and client.get("status") != "archived"
    ):
        remaining = (
            renewal - today
        ).days

        if remaining < 0:
            alerts.append(
                {
                    "kind": "renewal",
                    "level": "attention",
                    "message": (
                        f"The client renewal date was {_human_date(renewal)} "
                        f"({_duration_days(-remaining)} ago)."
                    ),
                }
            )

        elif remaining <= 3:
            alerts.append(
                {
                    "kind": "renewal",
                    "level": "urgent",
                    "message": (
                        f"Renewal is "
                        f"{'today' if remaining == 0 else 'in ' + _duration_days(remaining)} "
                        f"({_human_date(renewal)})."
                    ),
                }
            )

        elif remaining <= 14:
            alerts.append(
                {
                    "kind": "renewal",
                    "level": "upcoming",
                    "message": (
                        f"Renewal is in {_duration_days(remaining)} "
                        f"({_human_date(renewal)})."
                    ),
                }
            )

    last_month = _previous_month(today)

    published = {
        item.get("period")
        for item in history
    }

    try:
        started = date.fromisoformat(
            client.get(
                "collaboration_started",
                "",
            )
        )

    except (TypeError, ValueError):
        started = None

    if (
        client.get("status") == "active"
        and started is not None
        and started.strftime("%Y-%m") <= last_month
        and last_month not in published
    ):
        alerts.append(
            {
                "kind": "reports_due",
                "level": "attention",
                "message": (
                    "No published report is recorded "
                    f"for {_human_month(last_month)}."
                ),
            }
        )

    # De-duplicate published months.
    # A single month cannot count twice.
    by_period = {}

    for item in history:
        period = item.get("period")

        if (
            isinstance(period, str)
            and len(period) == 7
        ):
            by_period[period] = item

    rows = [
        by_period[key]
        for key in sorted(by_period)
    ]

    if (
        len(rows) >= 2
        and _aligned(rows[-2:])
    ):
        prior, current = rows[-2:]

        previous_median = prior[
            "median_impressions"
        ]

        if (
            previous_median > 0
            and current["median_impressions"]
            <= previous_median
            * (1 - DECLINE_FRACTION)
        ):
            alerts.append(
                {
                    "kind": "early_signal",
                    "level": "informational",
                    "message": (
                        "Early signal: median observed "
                        "post impressions fell at least "
                        "20% from the previous comparable "
                        "month. Review the context before "
                        "drawing conclusions."
                    ),
                }
            )

    if (
        len(rows) >= 5
        and _aligned(rows[-5:])
    ):
        five = rows[-5:]

        baseline = statistics.median(
            [
                row["median_impressions"]
                for row in five[:3]
            ]
        )

        if (
            baseline > 0
            and all(
                row["median_impressions"]
                <= baseline * (1 - DECLINE_FRACTION)
                for row in five[3:]
            )
        ):
            alerts.append(
                {
                    "kind": "performance",
                    "level": "attention",
                    "message": (
                        "Median observed post impressions "
                        "were at least 20% below the "
                        "three-month reference median in "
                        "each of two consecutive comparable "
                        "months. Descriptive only."
                    ),
                }
            )

    return alerts


class AlertService:
    def __init__(self, store):
        self.store = store

    def for_client(self, client_id):
        client = self.store.get_client(
            client_id
        )

        history, truncated = ReportingService(
            self.store
        ).history(client_id)

        alerts = evaluate_alerts(
            client,
            history,
        )

        if truncated:
            alerts.append(
                {
                    "kind": "coverage",
                    "level": "informational",
                    "message": (
                        "Older published reports are "
                        "omitted from this view."
                    ),
                }
            )

        return client, history, alerts
