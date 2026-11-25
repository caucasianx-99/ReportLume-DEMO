"""Confirmed CSV imports and immutable observations."""

import re

from datetime import (
    date,
    datetime,
    time,
    timedelta,
    timezone,
)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.firestore_rest import (
    FirestoreConflict,
    FirestoreNotFound,
    decode_fields,
    encode_fields,
)
from app.services.client_workspace import (
    WorkspaceReadOnly,
    has_paid_access,
    make_update,
    require_write_access,
)
from app.services.x_content_csv import METRICS
from app.services.observation_identity import observation_id


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")


class ImportValidationError(ValueError):
    pass


class ImportConflict(RuntimeError):
    pass


class PlanSelectionRequired(WorkspaceReadOnly):
    """A new trial needs a chosen plan before the first CSV import."""


MAX_DATE_CHANGES_SHOWN = 20


def _date(value: str, label: str) -> date:
    if not DATE_RE.fullmatch(value):
        raise ImportValidationError(
            f"{label} must use YYYY-MM-DD."
        )

    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ImportValidationError(
            f"{label} is not a valid date."
        ) from exc


def import_context(headers, parsed: dict) -> dict:
    start = _date(
        headers.get("x-export-start", ""),
        "Export start",
    )
    end = _date(
        headers.get("x-export-end", ""),
        "Export end",
    )
    measured = _date(
        headers.get("x-measurement-date", ""),
        "Measurement date",
    )

    zone_name = headers.get("x-measurement-zone", "")
    raw_time = headers.get(
        "x-measurement-time", ""
    ).strip()

    if (
        len(zone_name) > 64
        or not zone_name
        or any(c.isspace() for c in zone_name)
    ):
        raise ImportValidationError(
            "Choose a valid IANA time zone."
        )

    try:
        zone = ZoneInfo(zone_name)
    except (
        ZoneInfoNotFoundError,
        ValueError,
    ) as exc:
        raise ImportValidationError(
            "Unknown time zone."
        ) from exc

    if start > end:
        raise ImportValidationError(
            "Export start is after export end."
        )

    if measured < end:
        raise ImportValidationError(
            "Measurement date precedes export end."
        )

    if (
        parsed["published_start"] < start.isoformat()
        or parsed["published_end"] > end.isoformat()
    ):
        raise ImportValidationError(
            "Export dates do not cover every post in the CSV."
        )

    if measured > datetime.now(zone).date():
        raise ImportValidationError(
            "Measurement date cannot be in the future."
        )

    measurement_at = None

    if raw_time:
        if not TIME_RE.fullmatch(raw_time):
            raise ImportValidationError(
                "Measurement time must use HH:MM."
            )

        try:
            hour, minute = (
                int(value)
                for value in raw_time.split(":")
            )
            naive = datetime.combine(
                measured,
                time(hour, minute),
            )
        except ValueError as exc:
            raise ImportValidationError(
                "Invalid measurement clock time."
            ) from exc

        chosen = naive.replace(
            tzinfo=zone,
            fold=0,
        )
        other = naive.replace(
            tzinfo=zone,
            fold=1,
        )

        if chosen.utcoffset() != other.utcoffset():
            raise ImportValidationError(
                "Ambiguous local time: choose an unambiguous time."
            )

        utc_time = chosen.astimezone(timezone.utc)

        if (
            utc_time.astimezone(zone).replace(tzinfo=None)
            != naive
        ):
            raise ImportValidationError(
                "The clock time does not exist in this zone."
            )

        if utc_time > (
            datetime.now(timezone.utc)
            + timedelta(minutes=5)
        ):
            raise ImportValidationError(
                "Measurement time cannot be in the future."
            )

        measurement_at = utc_time.isoformat(
            timespec="minutes"
        )
        key = "minute:" + measurement_at
        precision = "confirmed_minute"

    else:
        key = (
            f"date_only:{measured.isoformat()}:"
            f"{zone_name}"
        )
        precision = "date_only_time_unknown"

    return {
        "export_start": start.isoformat(),
        "export_end": end.isoformat(),
        "measurement_date": measured.isoformat(),
        "measurement_time_zone": zone_name,
        "measurement_at": measurement_at,
        "measurement_precision": precision,
        "measurement_key": key,
    }


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


def _upsert(name: str, values: dict) -> dict:
    return {
        "update": {
            "name": name,
            "fields": encode_fields(values),
        },
        "updateMask": {
            "fieldPaths": list(values),
        },
    }


def _create(name: str, values: dict) -> dict:
    return {
        "update": {
            "name": name,
            "fields": encode_fields(values),
        },
        "currentDocument": {
            "exists": False,
        },
    }


class CsvImportService:

    def __init__(self, store):
        self.store = store

    def _check_access(self, client_id: str):
        profile, _ = self.store.get_user_with_version()
        require_write_access(profile)

        if (
            profile.get("trial_used_once")
            and not profile.get("trial_started_at")
        ):
            raise WorkspaceReadOnly(
                "Trial cannot be restarted."
            )

        # Existing pre-migration trials can finish without a selection,
        # but a NEW trial cannot begin without choosing a plan.
        if (
            not has_paid_access(profile)
            and not profile.get("trial_started_at")
            and profile.get("selected_plan")
            not in {"solo", "professional", "agency"}
        ):
            raise PlanSelectionRequired(
                "Choose Solo, Professional or Agency on the billing page "
                "before your first CSV import."
            )

        client = self.store.get_client(client_id)

        if client.get("status") != "active":
            raise ImportValidationError(
                "Only active clients can receive new imports."
            )

        if (
            not has_paid_access(profile)
            and profile.get("unpaid_client_id") != client_id
        ):
            raise WorkspaceReadOnly(
                "This client is not covered by the free trial."
            )

        return profile

    def _upload_name(
        self,
        client_id: str,
        upload_id: str,
    ) -> str:
        return (
            self.store.client_name(client_id)
            + "/uploads/"
            + upload_id
        )

    def _post_name(
        self,
        client_id: str,
        post_id: str,
    ) -> str:
        return (
            self.store.client_name(client_id)
            + "/posts/"
            + post_id
        )

    def _observation_name(
        self,
        client_id: str,
        post_id: str,
        key: str,
        upload_id: str,
    ) -> str:
        digest = observation_id(key, upload_id)

        return (
            self._post_name(client_id, post_id)
            + "/observations/"
            + digest
        )

    def list_uploads(self, client_id: str):
        self.store.get_client(client_id)

        result = self.store.list_collection(
            self.store.client_name(client_id)
            + "/uploads",
            page_size=20,
            order_by="created_at desc",
        )

        uploads = []

        for document in result.get("documents", []):
            record = decode_fields(
                document.get("fields", {})
            )
            record["id"] = (
                document["name"].rsplit("/", 1)[-1]
            )
            uploads.append(record)

        return uploads

    def existing_posts(self, client_id: str, rows) -> dict:
        """Stored date, text and link of these posts, by post id."""
        names = {
            self._post_name(client_id, row["post_id"]): row["post_id"]
            for row in rows
        }
        found = {}
        ordered = list(names)

        for offset in range(0, len(ordered), 100):
            batch = self.store.batch_get_documents(ordered[offset:offset + 100])

            for name, document in batch.items():
                if document is None:
                    continue

                fields = decode_fields(document.get("fields", {}))

                if isinstance(fields.get("published_date"), str):
                    found[names[name]] = {
                        key: fields.get(key)
                        for key in ("published_date", "post_text", "post_url")
                    }

        return found

    def overlap(self, client_id: str, rows) -> dict:
        """Posts this file shares with earlier imports, and what changes.

        A later import keeps the newest measurement, text and link of a
        post. A changed publication date would move the post to another
        reporting month, so the user must see and accept it; a changed text
        or link is counted so the preview can say so. Published report
        versions never change.
        """
        existing = self.existing_posts(client_id, rows)
        changes = []
        seen = set()
        content_changed = set()

        for row in rows:
            post_id, published = row["post_id"], row["published_date"]
            stored = existing.get(post_id)

            if stored is None:
                continue

            if (
                stored["published_date"] != published
                and (post_id, published) not in seen
            ):
                seen.add((post_id, published))
                changes.append({
                    "post_id": post_id,
                    "previous": stored["published_date"],
                    "new": published,
                })

            # Rows offered only as duplicate choices carry no text or link.
            if any(
                key in row and stored[key] is not None and row[key] != stored[key]
                for key in ("post_text", "post_url")
            ):
                content_changed.add(post_id)

        return {
            "already_imported": len(existing),
            "date_change_count": len(changes),
            "date_changes": changes[:MAX_DATE_CHANGES_SHOWN],
            "content_change_count": len(content_changed),
        }

    def _get_existing(self, name: str):
        try:
            return self.store.get_document(name)
        except FirestoreNotFound:
            return None

    def _row_writes(
        self,
        client_id,
        row,
        context,
        upload_id,
    ):
        post_name = self._post_name(
            client_id,
            row["post_id"],
        )

        observation_name = self._observation_name(
            client_id,
            row["post_id"],
            context["measurement_key"],
            upload_id,
        )

        post_fields = {
            "owner_uid": self.store.uid,
            "post_id": row["post_id"],
            "published_date": row["published_date"],
            "original_date": row["original_date"],
            "post_text": row["post_text"],
            "post_url": row["post_url"],
            "last_seen_upload": upload_id,
        }

        observation_fields = {
            "owner_uid": self.store.uid,
            "source": "x_analytics",
            "upload_id": upload_id,
            "post_id": row["post_id"],
            **context,
            **{
                metric: row[metric]
                for metric in METRICS
            },
        }

        return [
            _upsert(post_name, post_fields),
            _create(observation_name, observation_fields),
        ]

    def _retry_one(
        self,
        client_id,
        row,
        context,
        upload_id,
    ):
        writes = self._row_writes(
            client_id,
            row,
            context,
            upload_id,
        )
        observation_name = writes[1]["update"]["name"]

        for attempt in range(2):
            existing = self._get_existing(
                observation_name
            )

            if existing:
                previous = decode_fields(
                    existing.get("fields", {})
                )
                expected = decode_fields(
                    writes[1]["update"]["fields"]
                )

                if all(
                    previous.get(key) == value
                    for key, value in expected.items()
                ):
                    return

                raise ImportConflict(
                    "A different observation already exists "
                    "for this file and post."
                )

            try:
                self.store.commit(writes)
                return

            except FirestoreConflict:
                if attempt == 1:
                    raise ImportConflict(
                        "Concurrent observation update; retry import."
                    )

    def confirm(
        self,
        client_id: str,
        parsed: dict,
        context: dict,
    ) -> dict:
        """Import only after server-verified preview approval."""

        self._check_access(client_id)

        upload_id = parsed["sha256"]
        upload_name = self._upload_name(
            client_id, upload_id
        )

        existing = self._get_existing(upload_name)

        if existing:
            info = decode_fields(
                existing.get("fields", {})
            )

            if info.get("status") == "completed":
                return {
                    "status": "already_imported",
                    "upload_id": upload_id,
                    "rows": info["row_count"],
                }

            for key in (
                "measurement_key",
                "export_start",
                "export_end",
                "row_count",
            ):
                expected = (
                    parsed["row_count"]
                    if key == "row_count"
                    else context[key]
                )

                if info.get(key) != expected:
                    raise ImportConflict(
                        "The same file is already being imported "
                        "with different metadata."
                    )
        else:
            fields = {
                "owner_uid": self.store.uid,
                "status": "processing",
                "source": "x_analytics",
                "csv_sha256": upload_id,
                "client_id": client_id,
                "row_count": parsed["row_count"],
                "published_start": parsed["published_start"],
                "published_end": parsed["published_end"],
                "created_at": _iso_now(),
                **context,
            }

            try:
                self.store.commit([
                    _create(upload_name, fields)
                ])
            except FirestoreConflict:
                raced = self._get_existing(upload_name)

                if raced is None:
                    raise ImportConflict(
                        "Concurrent import initialization; retry."
                    )

                info = decode_fields(
                    raced.get("fields", {})
                )

                if info.get("status") == "completed":
                    return {
                        "status": "already_imported",
                        "upload_id": upload_id,
                        "rows": info["row_count"],
                    }

                for key in (
                    "measurement_key",
                    "export_start",
                    "export_end",
                    "row_count",
                ):
                    expected = (
                        parsed["row_count"]
                        if key == "row_count"
                        else context[key]
                    )

                    if info.get(key) != expected:
                        raise ImportConflict(
                            "Another import uses this file "
                            "with different dates."
                        )

        # Incomplete imports stay "processing".
        # Reports must use only completed imports.
        for start in range(
            0,
            len(parsed["rows"]),
            8,
        ):
            chunk = parsed["rows"][
                start:start + 8
            ]

            writes = []

            for row in chunk:
                writes.extend(
                    self._row_writes(
                        client_id,
                        row,
                        context,
                        upload_id,
                    )
                )

            try:
                self.store.commit(writes)

            except FirestoreConflict:
                for row in chunk:
                    self._retry_one(
                        client_id,
                        row,
                        context,
                        upload_id,
                    )

        # Complete the upload and activate the
        # one-time trial atomically.
        for _ in range(5):
            profile, version = (
                self.store.get_user_with_version()
            )

            require_write_access(profile)

            if (
                profile.get("trial_used_once")
                and not profile.get("trial_started_at")
            ):
                raise WorkspaceReadOnly(
                    "Trial cannot be restarted."
                )

            if (
                not has_paid_access(profile)
                and not profile.get("trial_started_at")
                and profile.get("selected_plan")
                not in {"solo", "professional", "agency"}
            ):
                raise WorkspaceReadOnly(
                    "Choose a plan before the first CSV import."
                )

            client = self.store.get_client(client_id)

            if client.get("status") != "active":
                raise ImportValidationError(
                    "Client is no longer active."
                )

            if (
                not has_paid_access(profile)
                and profile.get("unpaid_client_id") != client_id
            ):
                raise WorkspaceReadOnly(
                    "Client is not eligible for this trial."
                )

            upload_document = (
                self.store.get_document(upload_name)
            )

            record = decode_fields(
                upload_document.get("fields", {})
            )

            if record.get("status") == "completed":
                return {
                    "status": "already_imported",
                    "upload_id": upload_id,
                    "rows": parsed["row_count"],
                }

            now = datetime.now(timezone.utc)
            updates = {
                "last_import_id": upload_id,
            }
            trial_started = False

            if (
                not has_paid_access(profile)
                and not profile.get("trial_started_at")
            ):
                if profile.get("trial_used_once"):
                    raise WorkspaceReadOnly(
                        "Trial cannot be restarted."
                    )

                updates.update({
                    "trial_started_at": now.isoformat(),
                    "trial_ends_at": (
                        now + timedelta(hours=72)
                    ).isoformat(),
                    "trial_used_once": True,
                })
                trial_started = True

            writes = [
                make_update(
                    self.store.user_name,
                    updates,
                    version,
                ),
                make_update(
                    upload_name,
                    {
                        "status": "completed",
                        "completed_at": now.isoformat(),
                    },
                    upload_document["updateTime"],
                ),
            ]

            try:
                self.store.commit(writes)
                return {
                    "status": "imported",
                    "upload_id": upload_id,
                    "rows": parsed["row_count"],
                    "trial_started": trial_started,
                }
            except FirestoreConflict:
                continue

        raise ImportConflict(
            "Concurrent finalization; retry the same file."
        )
