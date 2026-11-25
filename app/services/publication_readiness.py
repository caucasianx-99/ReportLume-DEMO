
"""Owner-facing, evidence-based review of one frozen report revision.

This check explains missing evidence rather than inventing values or silently
modifying a published snapshot. The owner must explicitly acknowledge this
review before the report action route allows publication.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.services.monthly_defaults import is_unfinished_month


@dataclass(frozen=True)
class ReadinessItem:
    key: str
    title: str
    status: str
    detail: str


def _whole_number(value: Any):
    return value if type(value) is int and value >= 0 else None


def publication_readiness(snapshot: dict, *, reference_time=None) -> dict:
    """Summarize warning conditions in the exact frozen revision.

    This is an informed-review checklist, not a claim of independent data
    verification. Returned statuses are ready, review, or missing.
    """

    if not isinstance(snapshot, dict):
        raise ValueError("A frozen report snapshot is required")

    observed = snapshot.get("observed") or {}
    proof = snapshot.get("proof") or {}
    writing = snapshot.get("writing") or {}
    items = []

    count = _whole_number(observed.get("post_count"))
    unknown = _whole_number(observed.get("unknown_classifications"))

    if count is None:
        items.append(ReadinessItem(
            "posts",
            "Imported posts",
            "review",
            "The number of included posts or replies is not available "
            "in this older report."
        ))
    elif count == 0:
        items.append(ReadinessItem(
            "posts",
            "Imported posts",
            "missing",
            "There are no imported posts or replies in this report version."
        ))
    else:
        items.append(ReadinessItem(
            "posts",
            "Imported posts",
            "ready",
            f"This report version includes {count} posts or replies "
            "from the selected month."
        ))

    if unknown is None:
        items.append(ReadinessItem(
            "classification",
            "Content classification",
            "review",
            "Classification coverage is not recorded in this revision."
        ))
    elif unknown:
        items.append(ReadinessItem(
            "classification",
            "Content classification",
            "review",
            f"{unknown} posts have unknown authorship or content type. "
            "Review their labels or explicitly accept the limitation."
        ))
    else:
        items.append(ReadinessItem(
            "classification",
            "Content classification",
            "ready",
            "All included content has a recorded author and post/reply label."
        ))

    deliverables = proof.get("deliverables") or []
    pending = []

    for entry in deliverables:
        if not isinstance(entry, dict):
            continue

        committed = _whole_number(entry.get("committed"))

        if committed and entry.get("delivered") is None:
            pending.append(
                str(entry.get("name") or "Deliverable")[:50]
            )

    if pending:
        items.append(ReadinessItem(
            "deliverables",
            "Agreed work delivered",
            "review",
            "Declared work is missing for: " + ", ".join(pending) + "."
        ))
    elif not deliverables:
        items.append(ReadinessItem(
            "deliverables",
            "Agreed work delivered",
            "review",
            "This revision does not include a deliverables breakdown."
        ))
    else:
        items.append(ReadinessItem(
            "deliverables",
            "Agreed work delivered",
            "ready",
            "Delivery counts are recorded for the agreed work listed here."
        ))

    metrics = observed.get("metrics") or {}
    missing_metrics = []

    for metric in ("impressions", "engagements"):
        value = metrics.get(metric) or observed.get(metric) or {}
        missing = _whole_number(value.get("missing"))

        if missing is not None and missing > 0:
            missing_metrics.append(metric)

    if missing_metrics:
        items.append(ReadinessItem(
            "coverage",
            "Metric completeness",
            "review",
            "Some posts have missing "
            + ", ".join(missing_metrics)
            + " values. Missing values remain missing, not zero."
        ))
    elif count is None or snapshot.get("schema_version", 1) < 2:
        items.append(ReadinessItem(
            "coverage",
            "Metric completeness",
            "review",
            "Full metric-coverage metadata is not available "
            "in this older revision."
        ))
    else:
        items.append(ReadinessItem(
            "coverage",
            "Metric completeness",
            "ready",
            "No missing values were recorded for the headline metrics."
        ))

    if not str(writing.get("executive_summary") or "").strip():
        items.append(ReadinessItem(
            "narrative",
            "Executive summary",
            "review",
            "Add an executive summary before sharing this report."
        ))
    else:
        items.append(ReadinessItem(
            "narrative",
            "Executive summary",
            "ready",
            "An executive summary is included."
        ))

    brand = snapshot.get("brand")

    brand_valid = (
        isinstance(brand, str)
        and bool(brand.strip())
        and brand.strip().casefold() != "your agency"
    )

    items.append(ReadinessItem(
        "brand",
        "Agency name",
        "ready" if brand_valid else "missing",
        "Your agency name is shown on this report."
        if brand_valid
        else "Set your actual agency name before publishing."
    ))

    period = snapshot.get("period")
    period_available = isinstance(period, str) and len(period) == 7

    unfinished = (
        is_unfinished_month(
            period,
            reference_time or datetime.now(timezone.utc),
        )
        if period_available
        else False
    )

    items.append(ReadinessItem(
        "reporting_month",
        "Reporting month",
        "review" if unfinished or not period_available else "ready",
        "This month has not ended. Confirm interim publishing."
        if unfinished
        else (
            "The reporting month is missing."
            if not period_available
            else "This is a completed calendar month."
        ),
    ))

    blockers = []

    if count is None or count <= 0:
        blockers.append("posts")

    if not brand_valid:
        blockers.append("brand")

    return {
        "items": [
            item.__dict__
            for item in items
        ],
        "warnings": sum(
            item.status != "ready"
            for item in items
        ),
        "has_posts": count is not None and count > 0,
        "incomplete_month": unfinished,
        "blocks_publishing": bool(blockers),
        "blocking_reasons": blockers,
    }
