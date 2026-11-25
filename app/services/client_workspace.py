"""Client workspace business logic.

The first real client may be created before the trial begins.
Only a verified billing entitlement can unlock paid capacity.
"""

import json
import re
import uuid

from datetime import date, datetime, timezone

from app.core.config import settings
from app.services.workspace_access import plan_entitlements

from app.core.firestore_rest import (
    TenantStore,
    FirestoreConflict,
    FirestoreNotFound,
    encode_fields,
)


HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,15}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CLIENT_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


class ClientValidationError(ValueError):
    pass


class ClientQuotaError(RuntimeError):
    pass


class WorkspaceReadOnly(RuntimeError):
    pass


class WorkspaceBusy(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_date(raw: str, required: bool = False):
    raw = (raw or "").strip()

    if not raw:
        if required:
            raise ClientValidationError(
                "Collaboration start date is required."
            )
        return None

    if not DATE_PATTERN.fullmatch(raw):
        raise ClientValidationError(
            "Enter dates as YYYY-MM-DD."
        )

    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ClientValidationError(
            "Enter a valid calendar date."
        ) from exc


def nonnegative_int(data: dict, key: str):
    raw = (data.get(key) or "").strip()

    if not raw:
        return 0

    if not raw.isascii() or not raw.isdigit():
        raise ClientValidationError(
            f"{key} must be a non-negative whole number."
        )

    value = int(raw)

    if value > 100000:
        raise ClientValidationError(
            f"{key} is too large."
        )

    return value


def optional_nonnegative_int(data: dict, key: str):
    raw = (data.get(key) or "").strip()

    if not raw:
        return None

    return nonnegative_int(data, key)


def parse_client_form(data: dict) -> dict:
    name = (data.get("display_name") or "").strip()

    if not 1 <= len(name) <= 100:
        raise ClientValidationError(
            "Client name must be 1 to 100 characters."
        )

    handle = (data.get("x_handle") or "").strip()
    handle = handle.removeprefix("@")

    if handle and not HANDLE_PATTERN.fullmatch(handle):
        raise ClientValidationError(
            "Enter a valid X username without spaces."
        )

    start = valid_date(
        data.get("collaboration_started", ""),
        required=True,
    )

    renewal = valid_date(
        data.get("renewal_date", "")
    )

    if renewal and renewal < start:
        raise ClientValidationError(
            "Renewal date cannot precede the start date."
        )

    notes = (data.get("notes") or "").strip()
    goal_notes = (data.get("goal_notes") or "").strip()

    if len(notes) > 2000 or len(goal_notes) > 1000:
        raise ClientValidationError(
            "Notes exceed the allowed length."
        )

    return {
        "display_name": name,
        "x_handle": handle,
        "collaboration_started": start.isoformat(),
        "renewal_date": (
            renewal.isoformat()
            if renewal
            else None
        ),
        "monthly_posts": nonnegative_int(
            data, "monthly_posts"
        ),
        "monthly_threads": nonnegative_int(
            data, "monthly_threads"
        ),
        "monthly_replies": nonnegative_int(
            data, "monthly_replies"
        ),
        "monthly_strategy_calls": nonnegative_int(
            data, "monthly_strategy_calls"
        ),
        "post_impressions_goal": optional_nonnegative_int(
            data, "post_impressions_goal"
        ),
        "goal_notes": goal_notes,
        "notes": notes,
    }


def parse_timestamp(value):
    if not value or not isinstance(value, str):
        return None

    try:
        timestamp = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if timestamp.tzinfo is None:
            return None

        return timestamp.astimezone(timezone.utc)
    except ValueError:
        return None


def has_paid_access(profile: dict) -> bool:
    """Grant writes only for a bound, verified paid entitlement.

    A limited past_due grace period is possible only when a verified
    subscription transition persisted its original deadline.
    """
    from app.services.workspace_access import billing_entitlement

    return billing_entitlement(
        profile,
        settings,
    )


def client_limit_for(profile: dict) -> int:
    """Return a verified capacity; pre-trial accounts retain one slot."""
    if has_paid_access(profile):
        return plan_entitlements(
            profile, settings
        )["client_limit"]

    return settings.trial_client_limit


def selection_required(profile: dict) -> bool:
    """A downgrade must not silently select which clients survive."""
    if not has_paid_access(profile):
        return False

    limit = client_limit_for(profile)

    try:
        count = int(
            profile.get("active_client_count", 0)
        )
    except (TypeError, ValueError):
        return True

    return (
        limit < 1
        or count > limit
        or bool(
            profile.get(
                "capacity_selection_kept_ids_json"
            )
        )
    )


def require_write_access(profile: dict) -> bool:
    """Return paid status or reject expired/revoked access."""

    if has_paid_access(profile):
        if selection_required(profile):
            raise WorkspaceReadOnly(
                "Your current plan has fewer active client slots. "
                "Choose the clients to keep before creating new work."
            )
        return True

    # A former paid account cannot silently fall back
    # to a fresh free workspace.
    if profile.get("ever_paid", False):
        raise WorkspaceReadOnly(
            "Your subscription is not active."
        )

    if profile.get("trial_started_at"):
        trial_end = parse_timestamp(
            profile.get("trial_ends_at")
        )

        if (
            trial_end is None
            or trial_end <= datetime.now(timezone.utc)
        ):
            raise WorkspaceReadOnly(
                "Your trial has ended. Data is read-only."
            )

    return False


def make_update(
    name: str,
    values: dict,
    version: str,
):
    return {
        "update": {
            "name": name,
            "fields": encode_fields(values),
        },
        "updateMask": {
            "fieldPaths": list(values.keys()),
        },
        "currentDocument": {
            "updateTime": version,
        },
    }


class ClientWorkspace:

    def __init__(self, store: TenantStore):
        self.store = store

    def list_clients(self, page_token: str = ""):
        return self.store.list_clients(page_token)

    def get_client(self, client_id: str):
        return self.store.get_client(client_id)

    def list_reports(self, client_id: str):
        return self.store.list_reports(client_id)

    def select_solo_clients(self, kept_ids):
        """Atomically keep selected Solo clients and archive all others.

        Only tenant-scoped client documents are considered. The user profile
        and every archived client have update-time preconditions, so a
        concurrent plan change or status edit cannot be silently overwritten.
        Reports, links, approvals and frozen snapshots are never deleted.
        """
        if (
            not isinstance(kept_ids, (list, tuple))
            or not kept_ids
            or len(kept_ids) > 3
            or any(
                not isinstance(identifier, str)
                or not CLIENT_ID_PATTERN.fullmatch(identifier)
                for identifier in kept_ids
            )
            or len(set(kept_ids)) != len(kept_ids)
        ):
            raise ClientValidationError(
                "Choose exactly three distinct clients to keep."
            )

        for _ in range(3):
            profile, version = self.store.get_user_with_version()
            rights = plan_entitlements(profile, settings)

            if not (
                rights.get("paid")
                and rights.get("active")
                and rights.get("plan") == "solo"
                and rights.get("client_limit") == 3
            ):
                raise WorkspaceReadOnly(
                    "Client selection is available only on an active Solo plan."
                )

            # Read every page, not just the current directory screen.
            all_clients = []
            page_token = ""
            seen_tokens = set()

            for _page in range(500):
                rows, next_token = self.store.list_clients(page_token)

                if not isinstance(rows, list) or not isinstance(
                    next_token, str
                ):
                    raise WorkspaceBusy(
                        "Client directory is incomplete."
                    )

                all_clients.extend(rows)

                if len(all_clients) > 10000:
                    raise WorkspaceBusy(
                        "Client directory exceeds its safety limit."
                    )

                if not next_token:
                    break

                if (
                    len(next_token) > 2048
                    or next_token == page_token
                    or next_token in seen_tokens
                ):
                    raise WorkspaceBusy(
                        "Client pagination did not advance."
                    )

                seen_tokens.add(page_token)
                page_token = next_token
            else:
                raise WorkspaceBusy(
                    "Client directory is incomplete."
                )

            eligible = {}

            for row in all_clients:
                if not isinstance(row, dict):
                    raise WorkspaceBusy(
                        "Client directory contains invalid data."
                    )

                identifier = row.get("id")

                if (
                    not isinstance(identifier, str)
                    or not CLIENT_ID_PATTERN.fullmatch(identifier)
                ):
                    raise WorkspaceBusy(
                        "Client directory contains an invalid ID."
                    )

                if identifier in eligible:
                    raise WorkspaceBusy(
                        "Duplicate client in directory."
                    )

                if row.get("status") in ("active", "paused"):
                    eligible[identifier] = row

            try:
                stored_count = int(
                    profile.get("active_client_count", 0)
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise WorkspaceBusy(
                    "Client quota needs reconciliation."
                ) from exc

            if stored_count != len(eligible):
                raise WorkspaceBusy(
                    "Client quota needs reconciliation."
                )

            if stored_count <= 3:
                if set(kept_ids) == set(eligible):
                    return

                raise ClientValidationError(
                    "There are no excess clients to archive."
                )

            if (
                len(kept_ids) != 3
                or not set(kept_ids).issubset(eligible)
            ):
                raise ClientValidationError(
                    "Select exactly three active or paused clients."
                )

            archived_ids = sorted(
                set(eligible).difference(kept_ids)
            )

            # Firestore's gateway intentionally caps commits at 20 writes.
            if len(archived_ids) + 1 > 20:
                raise WorkspaceBusy(
                    "Too many clients for one safe change; contact support."
                )

            changes = []

            for identifier in archived_ids:
                client = self.store.get_client(identifier)

                if (
                    client.get("status") not in ("active", "paused")
                    or client.get("deletion_started_at")
                    or (
                        client.get("owner_uid")
                        and client.get("owner_uid") != self.store.uid
                    )
                    or not client.get("_update_time")
                ):
                    raise WorkspaceBusy(
                        "Client status changed. Refresh and choose again."
                    )

                changes.append(
                    make_update(
                        self.store.client_name(identifier),
                        {
                            "status": "archived",
                            "updated_at": now_iso(),
                        },
                        client["_update_time"],
                    )
                )

            changes.append(
                make_update(
                    self.store.user_name,
                    {"active_client_count": 3},
                    version,
                )
            )

            try:
                self.store.commit(changes)
                return
            except FirestoreConflict:
                continue

        raise WorkspaceBusy(
            "The client selection changed concurrently. Please retry."
        )

    def select_capacity_clients(self, kept_ids=None):
        """Select exactly the verified paid capacity, archiving in safe batches.

        Never derive capacity from the browser. Each batch atomically writes
        at most 19 client documents and the quota count. A persisted selection
        marker survives request failure and prevents a different selection
        from racing the remaining archive batches. Existing reports/links
        are never changed. Returns True when complete and False when another
        invocation is necessary; the POST can safely retry after interruption.
        """
        for _attempt in range(4):
            profile, version = self.store.get_user_with_version()
            rights = plan_entitlements(profile, settings)
            limit = rights.get("client_limit", 0)

            if (
                not rights.get("paid")
                or not rights.get("active")
                or type(limit) is not int
                or not 1 <= limit <= 200
            ):
                raise WorkspaceReadOnly(
                    "Client selection requires a verified paid plan."
                )

            pinned = profile.get(
                "capacity_selection_kept_ids_json"
            )

            if pinned:
                if (
                    profile.get("capacity_selection_limit") != limit
                    or not isinstance(pinned, str)
                ):
                    raise WorkspaceBusy(
                        "The verified capacity changed during selection. "
                        "Contact support."
                    )

                try:
                    chosen = json.loads(pinned)
                except (ValueError, TypeError) as exc:
                    raise WorkspaceBusy(
                        "Invalid selection checkpoint."
                    ) from exc

                if kept_ids is not None and chosen != kept_ids:
                    raise ClientValidationError(
                        "The existing client selection must be completed first."
                    )
            else:
                chosen = kept_ids

            if (
                not isinstance(chosen, list)
                or len(chosen) != limit
                or any(
                    not isinstance(i, str)
                    or not CLIENT_ID_PATTERN.fullmatch(i)
                    for i in chosen
                )
                or len(set(chosen)) != len(chosen)
            ):
                raise ClientValidationError(
                    f"Choose exactly {limit} distinct clients to keep."
                )

            rows = []
            token = ""
            seen = set()

            for _page in range(500):
                page, following = self.store.list_clients(token)

                if not isinstance(page, list) or not isinstance(
                    following, str
                ):
                    raise WorkspaceBusy(
                        "Client directory is incomplete."
                    )

                rows.extend(page)

                if len(rows) > 10000:
                    raise WorkspaceBusy(
                        "Client directory exceeds safety limit."
                    )

                if not following:
                    break

                if (
                    len(following) > 2048
                    or following in seen
                    or following == token
                ):
                    raise WorkspaceBusy(
                        "Client directory did not advance."
                    )

                seen.add(token)
                token = following
            else:
                raise WorkspaceBusy(
                    "Client directory is incomplete."
                )

            active = {}

            for client in rows:
                if not isinstance(client, dict):
                    raise WorkspaceBusy(
                        "Invalid client directory record."
                    )

                identifier = client.get("id")

                if (
                    not isinstance(identifier, str)
                    or not CLIENT_ID_PATTERN.fullmatch(identifier)
                ):
                    raise WorkspaceBusy(
                        "Invalid client ID in directory."
                    )

                if identifier in active:
                    raise WorkspaceBusy(
                        "Duplicate client ID in directory."
                    )

                if client.get("status") in ("active", "paused"):
                    active[identifier] = client

            count = profile.get("active_client_count", 0)

            if type(count) is not int or count != len(active):
                raise WorkspaceBusy(
                    "Client count requires reconciliation."
                )

            if not set(chosen).issubset(active):
                raise WorkspaceBusy(
                    "A selected client changed status. Refresh the list."
                )

            if count <= limit:
                if pinned:
                    try:
                        self.store.commit([
                            make_update(
                                self.store.user_name,
                                {
                                    "capacity_selection_kept_ids_json": None,
                                    "capacity_selection_limit": None,
                                },
                                version,
                            )
                        ])
                        return True
                    except FirestoreConflict:
                        continue

                return True

            if not pinned:
                try:
                    self.store.commit([
                        make_update(
                            self.store.user_name,
                            {
                                "capacity_selection_kept_ids_json": json.dumps(chosen),
                                "capacity_selection_limit": limit,
                            },
                            version,
                        )
                    ])
                    # A checkpoint is established before any destructive state
                    # transition. The next attempt archives a bounded batch.
                    continue
                except FirestoreConflict:
                    continue

            to_archive = sorted(
                set(active) - set(chosen)
            )[:19]

            if not to_archive:
                raise WorkspaceBusy(
                    "No eligible clients to archive."
                )

            timestamp = now_iso()
            changes = []

            for identifier in to_archive:
                client = self.store.get_client(identifier)

                if (
                    client.get("status") not in ("active", "paused")
                    or client.get("deletion_started_at")
                    or (
                        client.get("owner_uid")
                        and client.get("owner_uid") != self.store.uid
                    )
                    or not client.get("_update_time")
                ):
                    raise WorkspaceBusy(
                        "Client status changed. Refresh and try again."
                    )

                changes.append(
                    make_update(
                        self.store.client_name(identifier),
                        {
                            "status": "archived",
                            "updated_at": timestamp,
                        },
                        client["_update_time"],
                    )
                )

            remaining = count - len(to_archive)
            values = {
                "active_client_count": remaining,
            }

            if remaining <= limit:
                values.update({
                    "capacity_selection_kept_ids_json": None,
                    "capacity_selection_limit": None,
                })

            changes.append(
                make_update(
                    self.store.user_name,
                    values,
                    version,
                )
            )

            try:
                self.store.commit(changes)
                return remaining <= limit
            except FirestoreConflict:
                continue

        raise WorkspaceBusy(
            "The client directory changed; please retry."
        )

    def create_client(
        self,
        payload: dict,
        client_id: str,
    ):
        if not CLIENT_ID_PATTERN.fullmatch(client_id):
            raise ClientValidationError(
                "Invalid form identifier."
            )

        if payload.get("authorization") != "on":
            raise ClientValidationError(
                "Confirm that you are authorized "
                "to process the client's analytics data."
            )

        values = parse_client_form(payload)

        for _ in range(3):
            profile, version = self.store.get_user_with_version()

            paid = require_write_access(profile)

            current_count = int(
                profile.get("active_client_count", 0)
            )

            limit = client_limit_for(profile)

            if current_count >= limit:
                raise ClientQuotaError(
                    f"Your current limit is {limit} active client(s)."
                )

            if (
                not paid
                and profile.get(
                    "unpaid_client_created",
                    False,
                )
            ):
                raise ClientQuotaError(
                    "The one-time free client allowance "
                    "has already been used."
                )

            timestamp = now_iso()

            client_values = {
                **values,
                "owner_uid": self.store.uid,
                "status": "active",
                "authorized_by_owner": True,
                "authorization_asserted_at": timestamp,
                "created_at": timestamp,
                "updated_at": timestamp,
            }

            user_update = {
                "active_client_count": current_count + 1,
            }

            if not paid:
                user_update.update({
                    "unpaid_client_created": True,
                    "unpaid_client_id": client_id,
                })

            writes = [
                {
                    "update": {
                        "name": self.store.client_name(client_id),
                        "fields": encode_fields(client_values),
                    },
                    "currentDocument": {
                        "exists": False,
                    },
                },
                make_update(
                    self.store.user_name,
                    user_update,
                    version,
                ),
            ]

            try:
                self.store.commit(writes)
                return client_id
            except FirestoreConflict:
                continue

        raise WorkspaceBusy(
            "Client creation conflicted with another request. Please retry."
        )

    def update_client(
        self,
        client_id: str,
        payload: dict,
    ):
        values = parse_client_form(payload)

        for _ in range(3):
            profile, _ = self.store.get_user_with_version()
            require_write_access(profile)

            client = self.store.get_client(client_id)

            if client.get("status") == "archived":
                raise WorkspaceReadOnly(
                    "Archived clients are read-only."
                )

            if (
                values["x_handle"] != client.get("x_handle")
                and payload.get("authorization") != "on"
            ):
                raise ClientValidationError(
                    "Confirm data authorization when "
                    "changing the X username."
                )

            values["updated_at"] = now_iso()

            if values["x_handle"] != client.get("x_handle"):
                values["authorization_asserted_at"] = now_iso()

            write = make_update(
                self.store.client_name(client_id),
                values,
                client["_update_time"],
            )

            try:
                self.store.commit([write])
                return
            except FirestoreConflict:
                continue

        raise WorkspaceBusy(
            "Client was changed by another request."
        )

    def change_status(
        self,
        client_id: str,
        new_status: str,
    ):
        if new_status not in (
            "active",
            "paused",
            "archived",
        ):
            raise ClientValidationError(
                "Invalid client status."
            )

        for _ in range(3):
            profile, user_version = self.store.get_user_with_version()

            # Archiving excess clients remains possible while Solo
            # selection is pending; other writes stay blocked.
            if (
                new_status == "archived"
                and has_paid_access(profile)
            ):
                paid = True
            else:
                paid = require_write_access(profile)

            client = self.store.get_client(client_id)
            old_status = client.get("status", "archived")

            if client.get("deletion_started_at"):
                raise WorkspaceReadOnly(
                    "Client deletion is in progress."
                )

            if old_status == new_status:
                return

            if (
                old_status == "archived"
                and new_status == "paused"
            ):
                raise ClientValidationError(
                    "Restore the client before pausing."
                )

            count = int(
                profile.get("active_client_count", 0)
            )

            delta = 0

            if old_status == "archived":
                delta = 1

            if new_status == "archived":
                delta = -1

            if delta > 0:
                limit = client_limit_for(profile)

                if count >= limit:
                    raise ClientQuotaError(
                        "No active client slot is available."
                    )

                if (
                    not paid
                    and profile.get("unpaid_client_id") != client_id
                ):
                    raise ClientQuotaError(
                        "This client is not eligible "
                        "for free reactivation."
                    )

            if count + delta < 0:
                raise WorkspaceBusy(
                    "Client quota requires reconciliation."
                )

            changes = {
                "status": new_status,
                "updated_at": now_iso(),
            }

            writes = [
                make_update(
                    self.store.client_name(client_id),
                    changes,
                    client["_update_time"],
                )
            ]

            if delta:
                writes.append(
                    make_update(
                        self.store.user_name,
                        {
                            "active_client_count": count + delta,
                        },
                        user_version,
                    )
                )

            try:
                self.store.commit(writes)
                return
            except FirestoreConflict:
                continue

        raise WorkspaceBusy(
            "Status changed concurrently. Please retry."
        )
