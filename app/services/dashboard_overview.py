"""Bounded, tenant-scoped dashboard overview.

Client cards are derived from indexed owner-scoped client metadata.
No per-client report or observation documents are fetched here.

This service explains access but does not grant it.
"""

from datetime import (
    date,
    datetime,
    timedelta,
    timezone,
)

from app.core.config import settings
from app.services.workspace_access import plan_entitlements

from app.services.client_workspace import (
    WorkspaceReadOnly,
    has_paid_access,
    parse_timestamp,
    require_write_access,
    selection_required,
)


MAX_SCAN_PAGES = 50
PAGE_SIZE = 20


def _int_at_least_zero(value):
    try:
        number = int(value)
    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return 0

    return max(number, 0)


def _month(value):
    if not isinstance(value, str) or len(value) != 7:
        return None

    try:
        parsed = date.fromisoformat(value + "-01")
    except ValueError:
        return None

    return parsed.strftime("%Y-%m")


def _iso_date(value):
    if not isinstance(value, str) or not value:
        return None

    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def access_summary(profile, now=None):
    """Return display state from existing billing and trial policy."""

    now = now or datetime.now(timezone.utc)

    if now.tzinfo is None:
        raise ValueError(
            "Dashboard reference time must be timezone-aware."
        )

    now = now.astimezone(timezone.utc)

    rights = plan_entitlements(
        profile,
        settings,
        now=now,
    )
    paid = rights["paid"]

    try:
        require_write_access(profile)
        editable = True
    except WorkspaceReadOnly:
        editable = False

    selection_pending = bool(
        paid and selection_required(profile)
    )

    if selection_pending:
        editable = False

    trial_end = parse_timestamp(
        profile.get("trial_ends_at")
    )
    trial_started = bool(
        profile.get("trial_started_at")
    )

    label = "Free workspace"
    description = "Editing, branding and reporting are free."
    remaining_hours = None

    return {
        "label": label,
        "description": description,
        "editable": editable,
        "client_selection_required": selection_pending,
        "paid": paid,
        "trial_ends_at": (
            trial_end.isoformat()
            if label == "Trial" and trial_end is not None
            else ""
        ),
        "trial_remaining_hours": remaining_hours,
        "plan": rights["plan"],
        "case_limit": rights["case_limit"],
        "active_limit": (
            rights["client_limit"]
            if paid
            else 1
        ),
        "trial_report_used": bool(
            profile.get("trial_report_client_id")
        ),
    }


def dashboard_summary(
    store,
    *,
    today=None,
    max_pages=MAX_SCAN_PAGES,
):
    """Build a bounded dashboard view from client-list metadata."""

    today = today or datetime.now(
        timezone.utc
    ).date()

    if not isinstance(today, date):
        raise ValueError(
            "Dashboard reference date is invalid."
        )

    if (
        type(max_pages) is not int
        or not 1 <= max_pages <= MAX_SCAN_PAGES
    ):
        raise ValueError(
            "Invalid dashboard scan budget."
        )

    profile, _ = store.get_user_with_version()
    access = access_summary(profile)

    previous_period = (
        today.replace(day=1)
        - timedelta(days=1)
    ).strftime("%Y-%m")

    active_rows = []
    other_status_count = 0
    token = ""
    visited = set()
    scanned = 0
    truncated = False

    for _ in range(max_pages):
        clients, next_token = (
            store.list_clients(token)
        )

        if (
            not isinstance(clients, list)
            or not isinstance(next_token, str)
        ):
            raise ValueError(
                "Invalid client listing."
            )

        scanned += len(clients)

        for client in clients:
            if not isinstance(client, dict):
                raise ValueError(
                    "Invalid client record."
                )

            client_id = client.get("id", "")

            if (
                not isinstance(client_id, str)
                or not client_id
            ):
                raise ValueError(
                    "Client ID missing."
                )

            if client.get("status") != "active":
                other_status_count += 1
                continue

            last_period = _month(
                client.get("last_published_period")
            )

            last_publish = (
                last_period
                or "Not yet published"
            )

            renewal = _iso_date(
                client.get("renewal_date")
            )

            renewal_days = (
                (renewal - today).days
                if renewal is not None
                else None
            )

            start_date = _iso_date(
                client.get("collaboration_started")
            )

            expected_report = (
                start_date is not None
                and start_date.strftime("%Y-%m")
                <= previous_period
            )

            if (
                renewal_days is not None
                and renewal_days <= 14
            ):
                condition = "Renewal review"
            elif last_period is None:
                condition = "No published report"
            elif (
                expected_report
                and last_period < previous_period
            ):
                condition = "Report due"
            else:
                condition = "Review monthly reporting"

            active_rows.append({
                "id": client_id,
                "display_name": str(
                    client.get("display_name")
                    or "Unnamed client"
                )[:100],
                "x_handle": str(
                    client.get("x_handle")
                    or ""
                )[:15],
                "last_published_period": last_period,
                "last_publish_label": last_publish,
                "renewal_date": (
                    renewal.isoformat()
                    if renewal is not None
                    else ""
                ),
                "renewal_days": renewal_days,
                "status_label": condition,
            })

        if not next_token:
            break

        if (
            len(next_token) > 2048
            or next_token == token
            or next_token in visited
        ):
            raise ValueError(
                "Client pagination did not advance."
            )

        visited.add(token)
        token = next_token
    else:
        truncated = bool(next_token)

    active_rows.sort(
        key=lambda row: (
            row["renewal_days"] is None,
            (
                row["renewal_days"]
                if row["renewal_days"] is not None
                else 100000
            ),
            row["display_name"].casefold(),
        )
    )

    needs_attention = sum(
        row["status_label"] != "Review monthly reporting"
        for row in active_rows
    )

    active_count = _int_at_least_zero(
        profile.get("active_client_count")
    )

    visible_limit = (
        access["active_limit"]
        if access["paid"]
        else max(
            1,
            settings.professional_active_client_limit,
        )
    )

    return {
        "profile": profile,
        "access": access,
        "clients": active_rows[:visible_limit],
        "active_count": active_count,
        "visible_active_count": len(active_rows),
        "archived_seen": other_status_count,
        "needs_attention": needs_attention,
        "listing_truncated": (
            truncated
            or len(active_rows) > visible_limit
            or active_count > len(active_rows)
        ),
        "scanned_records": scanned,
        "reporting_period": previous_period,
    }
