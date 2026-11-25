"""Scoped report links, reviewer feedback and separately consented case studies.

Bearer links prove possession of a secret, NOT the holder's legal identity.
"""

import hashlib
import re
import secrets
import uuid

from datetime import datetime, timedelta, timezone

from app.core.firestore_rest import (
    FirestoreConflict,
    FirestoreNotFound,
    FirestoreUnavailable,
    decode_fields,
    encode_fields,
)
from app.services.client_workspace import (
    WorkspaceReadOnly,
    make_update,
    has_paid_access,
    parse_timestamp,
)
from app.services.report_store import (
    ReportConflict,
    ReportStore,
    ReportValidation,
    create,
    now,
)
from app.services.proof_of_work import month_bounds
from app.services.workspace_access import plan_entitlements
from app.core.config import settings


TOKEN = re.compile(r"^[A-Za-z0-9_-]{40,100}$")
HEX = re.compile(r"^[0-9a-f]{64}$")
URL_OR_HANDLE = re.compile(
    r"(?:https?://|www\.|\S+@\S+|@[A-Za-z0-9_]+)",
    re.I,
)


def _digest(token):
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise FirestoreNotFound(
            "Invalid or expired link"
        )

    return hashlib.sha256(
        token.encode("ascii")
    ).hexdigest()


def _text(value, limit, label, allow_blank=True):
    if not isinstance(value, str) or len(value) > limit:
        raise ReportValidation(
            f"{label} must be at most {limit} characters."
        )

    result = value.strip()

    if not allow_blank and not result:
        raise ReportValidation(
            f"{label} is required."
        )

    if any(
        ord(character) < 32
        and character not in "\n\t"
        for character in result
    ):
        raise ReportValidation(
            f"Invalid control characters in {label}."
        )

    return result


def _active(fields):
    if fields.get("revoked"):
        raise FirestoreNotFound("Link revoked")

    try:
        expires = datetime.fromisoformat(
            fields["expires_at"].replace("Z", "+00:00")
        )

        if expires <= datetime.now(timezone.utc):
            raise FirestoreNotFound("Link expired")

    except (TypeError, ValueError, KeyError) as exc:
        raise FirestoreNotFound(
            "Invalid expiry"
        ) from exc


def _report_link_expiry(fields, first_paid_at=None):
    """Compute a link's effective expiry without modifying its record.

    Trial-created report links use the original short expiry before a
    verified payment. Once first_paid_at is established from qualified
    provider history, they can live through creation + 30 days.
    Missing legacy creation timestamps are not extended.
    """
    original = parse_timestamp(
        fields.get("expires_at")
    )

    if original is None or not fields.get("trial_only"):
        return original

    if parse_timestamp(first_paid_at) is None:
        return original

    created = parse_timestamp(
        fields.get("created_at")
    )

    if created is None:
        return original

    return created + timedelta(days=30)


class SharingService:

    def __init__(self, store):
        self.store = store
        self.reports = ReportStore(store)

    def _share_name(self, client_id, digest):
        if not HEX.fullmatch(digest):
            raise ReportValidation(
                "Invalid share identifier"
            )

        return (
            self.store.client_name(client_id)
            + "/shares/"
            + digest
        )

    def _monthly_records(
        self,
        client_id,
        collection,
        period,
    ):
        """Read a bounded tenant/month set before ordering it.

        Firestore orders by document name, avoiding a compound date index.
        The results are sorted after owner/client/period validation.
        """
        month_bounds(period)

        if collection not in ("shares", "reviews"):
            raise ReportValidation(
                "Invalid monthly record collection."
            )

        parent = self.store.client_name(client_id)
        query = getattr(self.store, "run_query", None)
        documents = []

        if callable(query):
            batch_size = 250
            last_name = None

            while True:
                statement = {
                    "from": [
                        {"collectionId": collection}
                    ],
                    "where": {
                        "fieldFilter": {
                            "field": {
                                "fieldPath": "period"
                            },
                            "op": "EQUAL",
                            "value": {
                                "stringValue": period
                            },
                        }
                    },
                    "orderBy": [
                        {
                            "field": {
                                "fieldPath": "__name__"
                            },
                            "direction": "ASCENDING",
                        }
                    ],
                    "limit": batch_size,
                }

                if last_name is not None:
                    statement["startAt"] = {
                        "values": [
                            {
                                "referenceValue": last_name
                            }
                        ],
                        "before": False,
                    }

                response = query(
                    parent,
                    statement,
                )

                batch = [
                    row["document"]
                    for row in response
                    if "document" in row
                ]

                if len(batch) > batch_size:
                    raise ReportConflict(
                        "Monthly query exceeded its page size."
                    )

                for doc in batch:
                    name = doc.get("name", "")

                    if (
                        not isinstance(name, str)
                        or (
                            last_name is not None
                            and name <= last_name
                        )
                    ):
                        raise ReportConflict(
                            "Monthly query pagination did not advance."
                        )

                    documents.append(doc)
                    last_name = name

                    if len(documents) > 10000:
                        raise ReportConflict(
                            "Monthly record history exceeds the supported limit."
                        )

                if len(batch) < batch_size:
                    break

        else:
            # Offline adapters use the same scope and record limit.
            path = parent + "/" + collection
            token = ""
            visited = set()

            while True:
                response = self.store.list_collection(
                    path,
                    page_size=50,
                    page_token=token,
                )

                documents.extend(
                    response.get("documents", [])
                )

                if len(documents) > 10000:
                    raise ReportConflict(
                        "Monthly record history exceeds the supported limit."
                    )

                next_token = response.get(
                    "nextPageToken"
                )

                if not next_token:
                    break

                if (
                    next_token == token
                    or next_token in visited
                ):
                    raise ReportConflict(
                        "Monthly record pagination repeated a cursor."
                    )

                visited.add(next_token)
                token = next_token

        matched = []
        prefix = (
            parent + "/" + collection + "/"
        )

        for doc in documents:
            name = doc.get("name", "")

            if (
                not isinstance(name, str)
                or not name.startswith(prefix)
                or "/" in name[len(prefix):]
            ):
                raise ReportConflict(
                    "Unexpected monthly record path."
                )

            fields = decode_fields(
                doc.get("fields", {})
            )

            if fields.get("period") != period:
                if callable(query):
                    raise ReportConflict(
                        "Unexpected month in scoped query."
                    )
                continue

            if (
                fields.get("owner_uid")
                not in (None, self.store.uid)
                or fields.get("client_id")
                not in (None, client_id)
            ):
                raise ReportConflict(
                    "Unexpected scoped record in month query."
                )

            matched.append({
                "id": name.rsplit("/", 1)[-1],
                **fields,
            })

        timestamp_key = (
            "created_at"
            if collection == "shares"
            else "submitted_at"
        )

        matched.sort(
            key=lambda row: (
                str(row.get(timestamp_key) or ""),
                row["id"],
            ),
            reverse=True,
        )

        return matched

    def _period_records(
        self,
        client_id,
        collection,
        period,
        *,
        limit=50,
        page=1,
    ):
        """Page monthly data after scope validation and sorting."""
        if type(page) is not int or not 1 <= page <= 200:
            raise ReportValidation(
                "Invalid result page."
            )

        if type(limit) is not int or not 1 <= limit <= 50:
            raise ReportValidation(
                "Invalid result size."
            )

        rows = self._monthly_records(
            client_id,
            collection,
            period,
        )
        offset = (page - 1) * limit

        return (
            rows[offset:offset + limit],
            len(rows) > offset + limit,
        )

    def create_link(self, client_id, period, role):
        if role not in ("viewer", "reviewer"):
            raise ReportValidation(
                "Invalid share scope"
            )

        self.reports._can_write(client_id)

        parent, _ = self.reports.get_parent(
            client_id,
            period,
        )

        revision = parent.get(
            "published_revision"
        )

        if not revision:
            raise ReportValidation(
                "Publish a report before sharing it."
            )

        self.reports.get_revision(
            client_id,
            period,
            revision,
        )

        token = secrets.token_urlsafe(32)
        name = self._share_name(
            client_id,
            _digest(token),
        )

        profile, _ = self.store.get_user_with_version()
        trial_only = not has_paid_access(profile)
        created = datetime.now(timezone.utc)
        expires = created + timedelta(days=30)

        if trial_only:
            end = parse_timestamp(
                profile.get("trial_ends_at")
            )

            if (
                end is None
                or end <= datetime.now(timezone.utc)
            ):
                raise WorkspaceReadOnly(
                    "A confirmed active trial is required."
                )

            expires = min(expires, end)

        self.store.commit([
            create(
                name,
                {
                    "owner_uid": self.store.uid,
                    "client_id": client_id,
                    "period": period,
                    "revision_id": revision,
                    "role": role,
                    "created_at": created.isoformat(),
                    "expires_at": expires.isoformat(),
                    "trial_only": trial_only,
                    "revoked": False,
                    "submission_count": 0,
                },
            )
        ])

        return token

    def verify(self, client_id, token):
        document = self.store.get_document(
            self._share_name(
                client_id,
                _digest(token),
            )
        )
        fields = decode_fields(
            document.get("fields", {})
        )

        if fields.get("revoked"):
            raise FirestoreNotFound(
                "Link revoked"
            )

        first_paid_at = None

        if fields.get("trial_only"):
            profile, _ = self.store.get_user_with_version()
            first_paid_at = profile.get("first_paid_at")

        deadline = _report_link_expiry(
            fields,
            first_paid_at,
        )

        if (
            deadline is None
            or deadline <= datetime.now(timezone.utc)
        ):
            raise FirestoreNotFound(
                "Link expired"
            )

        fields["effective_expires_at"] = (
            deadline.isoformat()
        )

        if (
            fields.get("owner_uid") != self.store.uid
            or fields.get("client_id") != client_id
        ):
            raise FirestoreNotFound(
                "Link scope invalid"
            )

        _, snapshot = self.reports.get_revision(
            client_id,
            fields["period"],
            fields["revision_id"],
        )

        return (
            fields,
            snapshot,
            document["updateTime"],
        )

    def _link_states(self, rows):
        """Compute live state from expiry and irrevocable revocation."""
        profile, _ = self.store.get_user_with_version()
        first_paid_at = profile.get("first_paid_at")
        today = datetime.now(timezone.utc)

        for row in rows:
            expires = _report_link_expiry(
                row,
                (
                    first_paid_at
                    if row.get("trial_only")
                    else None
                ),
            )

            row["effective_expires_at"] = (
                expires.isoformat()
                if expires is not None
                else ""
            )

            row["link_state"] = (
                "Revoked"
                if row.get("revoked")
                else "Expired"
                if expires is None or expires <= today
                else "Active"
            )

        return rows

    def month_links(self, client_id, period):
        """All monthly links, including expired or revoked links."""
        self.store.get_client(client_id)

        rows = self._monthly_records(
            client_id,
            "shares",
            period,
        )

        return self._link_states(rows)

    def list_links(
        self,
        client_id,
        limit=50,
        *,
        period=None,
        page=1,
    ):
        self.store.get_client(client_id)

        if type(page) is not int or not 1 <= page <= 200:
            raise ReportValidation(
                "Invalid result page."
            )

        if type(limit) is not int or not 1 <= limit <= 50:
            raise ReportValidation(
                "Invalid result size."
            )

        if period is None:
            path = (
                self.store.client_name(client_id)
                + "/shares"
            )

            rows = []
            token = ""
            visited = set()

            while True:
                response = self.store.list_collection(
                    path,
                    page_size=50,
                    page_token=token,
                )

                rows.extend({
                    "id": doc["name"].rsplit("/", 1)[-1],
                    **decode_fields(
                        doc.get("fields", {})
                    ),
                } for doc in response.get("documents", []))

                if len(rows) > 10000:
                    raise ReportConflict(
                        "Could not read link history."
                    )

                next_token = response.get(
                    "nextPageToken"
                )

                if not next_token:
                    break

                if (
                    next_token == token
                    or next_token in visited
                ):
                    raise ReportConflict(
                        "Could not read link history."
                    )

                visited.add(next_token)
                token = next_token

            rows.sort(
                key=lambda row: (
                    str(row.get("created_at") or ""),
                    row["id"],
                ),
                reverse=True,
            )
        else:
            rows = self.month_links(
                client_id,
                period,
            )

        offset = (page - 1) * limit
        more = len(rows) > offset + limit

        return (
            self._link_states(
                rows[offset:offset + limit]
            ),
            more,
        )

    def revoke(self, client_id, digest):
        self.store.get_client(client_id)

        name = self._share_name(
            client_id,
            digest,
        )

        doc = self.store.get_document(name)

        self.store.commit([
            make_update(
                name,
                {"revoked": True},
                doc["updateTime"],
            )
        ])

    def submit(self, client_id, token, data):
        fields, _, version = self.verify(
            client_id,
            token,
        )

        if fields.get("role") != "reviewer":
            raise WorkspaceReadOnly(
                "Link does not allow feedback."
            )

        action = data.get("action")

        if action not in ("comment", "approve"):
            raise ReportValidation(
                "Invalid feedback action."
            )

        if (
            action == "approve"
            and data.get("confirm_approval") != "on"
        ):
            raise ReportValidation(
                "Explicit approval acknowledgement required."
            )

        name = _text(
            data.get("reviewer_name", ""),
            80,
            "Reviewer name",
            False,
        )

        comment = _text(
            data.get("comment", ""),
            2000,
            "Comment",
            action != "comment",
        )

        if int(fields.get("submission_count", 0)) >= 20:
            raise WorkspaceReadOnly(
                "Feedback limit for this link reached."
            )

        # Link possession does not verify legal identity or marketing consent.
        record_name = (
            self.store.client_name(client_id)
            + "/reviews/"
            + uuid.uuid4().hex
        )

        share_name = self._share_name(
            client_id,
            _digest(token),
        )

        self.store.commit([
            create(
                record_name,
                {
                    "owner_uid": self.store.uid,
                    "client_id": client_id,
                    "period": fields["period"],
                    "revision_id": fields["revision_id"],
                    "action": action,
                    "reviewer_name": name,
                    "comment": comment,
                    "identity_status": "unverified_link_holder",
                    "marketing_consent": False,
                    "submitted_at": now(),
                },
            ),
            make_update(
                share_name,
                {
                    "submission_count": (
                        int(fields["submission_count"]) + 1
                    ),
                },
                version,
            ),
        ])

    def feedback(
        self,
        client_id,
        period,
        revision=None,
        limit=50,
        *,
        page=1,
    ):
        """Read month-scoped feedback, newest first within every version."""
        self.store.get_client(client_id)

        if revision is not None and (
            not isinstance(revision, str)
            or not re.fullmatch(
                r"[a-f0-9]{32}",
                revision,
            )
        ):
            raise ReportValidation(
                "Invalid feedback revision."
            )

        if type(page) is not int or not 1 <= page <= 200:
            raise ReportValidation(
                "Invalid result page."
            )

        if type(limit) is not int or not 1 <= limit <= 50:
            raise ReportValidation(
                "Invalid result size."
            )

        rows = self._monthly_records(
            client_id,
            "reviews",
            period,
        )

        if revision is not None:
            rows = [
                row
                for row in rows
                if row.get("revision_id") == revision
            ]

        offset = (page - 1) * limit

        return (
            rows[offset:offset + limit],
            len(rows) > offset + limit,
        )


class CaseService:
    """One anonymized case draft per client/reporting month."""

    def __init__(self, store):
        self.store = store
        self.reports = ReportStore(store)

    def _name(self, client_id, period):
        month_bounds(period)

        return (
            self.store.client_name(client_id)
            + "/cases/"
            + period
        )

    def _link_name(self, client_id, token):
        return (
            self.store.client_name(client_id)
            + "/case_links/"
            + _digest(token)
        )

    def get(self, client_id, period):
        self.store.get_client(client_id)

        try:
            document = self.store.get_document(
                self._name(client_id, period)
            )
        except FirestoreNotFound:
            return ({}, "")

        return (
            decode_fields(
                document.get("fields", {})
            ),
            document["updateTime"],
        )

    def _catalog(self):
        """Read bounded owner-scoped case inventory, including archives."""
        records = []
        cursor = ""
        seen = set()
        checked_clients = set()

        for _ in range(500):
            clients, next_cursor = (
                self.store.list_clients(cursor)
            )

            if (
                not isinstance(clients, list)
                or not isinstance(next_cursor, str)
            ):
                raise FirestoreUnavailable(
                    "Case directory is incomplete."
                )

            for client in clients:
                client_id = (
                    client.get("id")
                    if isinstance(client, dict)
                    else None
                )

                if (
                    not isinstance(client_id, str)
                    or not client_id
                    or client_id in checked_clients
                ):
                    raise FirestoreUnavailable(
                        "Invalid case directory."
                    )

                checked_clients.add(client_id)

                if len(checked_clients) > 10000:
                    raise FirestoreUnavailable(
                        "Case directory is too large."
                    )

                folder = (
                    self.store.client_name(client_id)
                    + "/cases"
                )

                page_token = ""
                tokens = set()

                for _page in range(200):
                    page = self.store.list_collection(
                        folder,
                        page_size=50,
                        page_token=page_token,
                    )

                    documents = page.get(
                        "documents", []
                    )

                    if (
                        not isinstance(documents, list)
                        or len(documents) > 50
                    ):
                        raise FirestoreUnavailable(
                            "Case history is incomplete."
                        )

                    for document in documents:
                        name = document.get(
                            "name", ""
                        )

                        if (
                            not isinstance(name, str)
                            or not name.startswith(folder + "/")
                            or "/" in name[len(folder) + 1:]
                        ):
                            raise FirestoreUnavailable(
                                "Unexpected case record."
                            )

                        fields = decode_fields(
                            document.get("fields", {})
                        )

                        if (
                            fields.get("owner_uid")
                            not in (None, self.store.uid)
                            or fields.get("client_id")
                            not in (None, client_id)
                            or fields.get(
                                "period",
                                name.rsplit("/", 1)[-1],
                            )
                            != name.rsplit("/", 1)[-1]
                        ):
                            raise FirestoreUnavailable(
                                "Unexpected case scope."
                            )

                        records.append({
                            **fields,
                            "client_id": client_id,
                            "period": name.rsplit("/", 1)[-1],
                            "_name": name,
                        })

                        if len(records) > 10000:
                            raise FirestoreUnavailable(
                                "Case history is too large."
                            )

                    next_page = page.get(
                        "nextPageToken", ""
                    )

                    if not next_page:
                        break

                    if (
                        not isinstance(next_page, str)
                        or len(next_page) > 2048
                        or next_page == page_token
                        or next_page in tokens
                    ):
                        raise FirestoreUnavailable(
                            "Case paging did not advance."
                        )

                    tokens.add(page_token)
                    page_token = next_page
                else:
                    raise FirestoreUnavailable(
                        "Case history is incomplete."
                    )

            if not next_cursor:
                return records

            if (
                len(next_cursor) > 2048
                or next_cursor == cursor
                or next_cursor in seen
            ):
                raise FirestoreUnavailable(
                    "Client paging did not advance."
                )

            seen.add(cursor)
            cursor = next_cursor

        raise FirestoreUnavailable(
            "Case directory is incomplete."
        )

    @staticmethod
    def _solo_retained(cases, profile):
        """Find one retained case without reviving older hidden cases."""
        candidates = [
            item
            for item in cases
            if parse_timestamp(
                item.get("published_at")
            )
        ]

        chosen = (
            profile.get("retained_case_client_id"),
            profile.get("retained_case_period"),
        )

        for item in candidates:
            if (
                item["client_id"],
                item["period"],
            ) == chosen:
                return (
                    item["client_id"],
                    item["period"],
                )

        if not candidates:
            return None

        latest = max(
            candidates,
            key=lambda item: (
                parse_timestamp(
                    item["published_at"]
                ),
                item["_name"],
            ),
        )

        return (
            latest["client_id"],
            latest["period"],
        )

    @staticmethod
    def _trial_case_withdrawn(case, profile, *, current):
        """An expired unpaid trial cannot silently republish after payment."""
        if not case.get("trial_only"):
            return False

        end = parse_timestamp(
            profile.get("trial_ends_at")
        )

        if end is None:
            return True

        if current < end:
            return False

        first_paid = parse_timestamp(
            profile.get("first_paid_at")
        )

        if first_paid is not None and first_paid < end:
            return False

        published_at = parse_timestamp(
            case.get("published_at")
        )

        return (
            published_at is None
            or published_at <= end
        )

    def visibility(
        self,
        client_id,
        period,
        *,
        case=None,
        profile=None,
    ):
        """Derive public status from current entitlements and consent."""
        if case is None:
            case, _ = self.get(client_id, period)

        if not case.get("draft_hash"):
            return {
                "public": False,
                "label": "Not created",
            }

        if (
            case.get("state") != "published"
            or not case.get("consent_hash")
            or case.get("consent_hash")
            != case.get("draft_hash")
        ):
            return {
                "public": False,
                "label": "Draft — not public",
            }

        if profile is None:
            profile, _ = self.store.get_user_with_version()

        current = datetime.now(timezone.utc)
        rights = plan_entitlements(
            profile,
            settings,
            now=current,
        )

        if not rights["active"]:
            return {
                "public": False,
                "label": (
                    "Hidden — trial or subscription "
                    "access has ended"
                ),
            }

        if self._trial_case_withdrawn(
            case,
            profile,
            current=current,
        ):
            return {
                "public": False,
                "label": (
                    "Hidden — publish again after "
                    "the expired trial"
                ),
            }

        if rights["plan"] == "solo":
            retained = self._solo_retained(
                self._catalog(),
                profile,
            )

            if retained != (client_id, period):
                return {
                    "public": False,
                    "label": (
                        "Hidden — Solo allows "
                        "one public case study"
                    ),
                }

        return {
            "public": True,
            "label": "Published",
        }

    def retain(self, client_id, period):
        """Store an explicit case choice without editing its content."""
        case, version = self.get(client_id, period)

        if (
            not version
            or case.get("state") != "published"
            or not case.get("draft_hash")
            or case.get("consent_hash")
            != case.get("draft_hash")
        ):
            raise ReportValidation(
                "Choose a published, approved case study."
            )

        profile, profile_version = (
            self.store.get_user_with_version()
        )
        rights = plan_entitlements(
            profile,
            settings,
        )

        if (
            not rights["active"]
            or rights["plan"]
            not in ("solo", "professional", "agency")
        ):
            raise WorkspaceReadOnly(
                "An active plan is required."
            )

        if self._trial_case_withdrawn(
            case,
            profile,
            current=datetime.now(timezone.utc),
        ):
            raise WorkspaceReadOnly(
                "Publish this case study again before selecting it."
            )

        if (
            profile.get("retained_case_client_id") == client_id
            and profile.get("retained_case_period") == period
        ):
            return

        try:
            self.store.commit([
                make_update(
                    self.store.user_name,
                    {
                        "retained_case_client_id": client_id,
                        "retained_case_period": period,
                    },
                    profile_version,
                )
            ])
        except FirestoreConflict as exc:
            raise ReportConflict(
                "The case-study selection changed. Refresh and retry."
            ) from exc

    def save(self, client_id, period, raw, expected_version):
        self.reports._can_write(client_id)

        parent, _ = self.reports.get_parent(
            client_id,
            period,
        )
        revision = parent.get("published_revision")

        if not revision:
            raise ReportValidation(
                "A published report is required for a case study."
            )

        _, snapshot = self.reports.get_revision(
            client_id,
            period,
            revision,
        )

        fields = {}

        for key, limit in (
            ("headline", 120),
            ("context", 1000),
            ("approach", 1000),
            ("outcome", 1000),
        ):
            fields[key] = _text(
                raw.get(key, ""),
                limit,
                key,
                False,
            )

            if URL_OR_HANDLE.search(fields[key]):
                raise ReportValidation(
                    "Remove URLs, email addresses and @handles "
                    "from case text."
                )

        fields.update({
            "period": period,
            "report_revision": revision,
            "trial_only": bool(
                snapshot.get("trial_watermark", True)
            ),
            "sample_size": (
                snapshot["observed"]["impressions"]["n"]
            ),
            "median_post_impressions": (
                snapshot["observed"]["impressions"]["median"]
            ),
            "limitation": (
                "Descriptive cumulative post observation; "
                "not attributable growth."
            ),
            "owner_uid": self.store.uid,
            "client_id": client_id,
            "state": "draft",
            "consent_hash": None,
            "consented_at": None,
            "consent_actor": None,
            "updated_at": now(),
        })

        text_to_sign = "\n".join(
            str(fields[key])
            for key in (
                "headline",
                "context",
                "approach",
                "outcome",
                "period",
                "report_revision",
                "sample_size",
                "median_post_impressions",
            )
        )

        fields["draft_hash"] = hashlib.sha256(
            text_to_sign.encode("utf-8")
        ).hexdigest()

        existing, version = self.get(
            client_id,
            period,
        )

        if (version or "") != (expected_version or ""):
            raise ReportConflict(
                "Case draft changed. Refresh and retry."
            )

        if version:
            write = make_update(
                self._name(client_id, period),
                fields,
                version,
            )
        else:
            write = create(
                self._name(client_id, period),
                fields,
            )

        try:
            self.store.commit([write])
        except FirestoreConflict as exc:
            raise ReportConflict(
                "Case draft changed concurrently."
            ) from exc

    def make_consent_link(self, client_id, period):
        self.reports._can_write(client_id)

        case, _ = self.get(
            client_id,
            period,
        )

        if not case.get("draft_hash"):
            raise ReportValidation(
                "Save a case draft first."
            )

        token = secrets.token_urlsafe(32)
        profile, _ = self.store.get_user_with_version()
        trial_only = not has_paid_access(profile)
        expires = (
            datetime.now(timezone.utc)
            + timedelta(days=30)
        )

        if trial_only:
            end = parse_timestamp(
                profile.get("trial_ends_at")
            )

            if (
                end is None
                or end <= datetime.now(timezone.utc)
            ):
                raise WorkspaceReadOnly(
                    "A confirmed active trial is required."
                )

            expires = min(expires, end)

        self.store.commit([
            create(
                self._link_name(client_id, token),
                {
                    "owner_uid": self.store.uid,
                    "client_id": client_id,
                    "period": period,
                    "draft_hash": case["draft_hash"],
                    "expires_at": expires.isoformat(),
                    "trial_only": trial_only,
                    "revoked": False,
                    "created_at": now(),
                },
            )
        ])

        return token

    def verify_consent_link(
        self,
        client_id,
        period,
        token,
    ):
        document = self.store.get_document(
            self._link_name(client_id, token)
        )

        fields = decode_fields(
            document.get("fields", {})
        )

        _active(fields)

        if (
            fields.get("owner_uid") != self.store.uid
            or fields.get("client_id") != client_id
            or fields.get("period") != period
        ):
            raise FirestoreNotFound(
                "Case link scope invalid"
            )

        case, version = self.get(
            client_id,
            period,
        )

        if (
            not version
            or case.get("draft_hash")
            != fields.get("draft_hash")
        ):
            raise FirestoreNotFound(
                "Case draft changed; consent link is obsolete"
            )

        return (
            case,
            version,
        )

    def consent(
        self,
        client_id,
        period,
        token,
        raw,
    ):
        case, version = self.verify_consent_link(
            client_id,
            period,
            token,
        )

        action = raw.get("action")

        if action == "withdraw":
            changes = {
                "consent_hash": None,
                "consented_at": None,
                "consent_actor": None,
                "state": "draft",
                "updated_at": now(),
            }

        elif action == "consent":
            if (
                raw.get("authorized") != "on"
                or raw.get("case_consent") != "on"
            ):
                raise ReportValidation(
                    "Explicit case-study marketing consent required."
                )

            name = _text(
                raw.get("consent_actor", ""),
                80,
                "Consent name",
                False,
            )

            changes = {
                "consent_hash": case["draft_hash"],
                "consented_at": now(),
                "consent_actor": name,
                "consent_origin": "unverified_link_holder",
                "updated_at": now(),
            }
        else:
            raise ReportValidation(
                "Invalid consent action"
            )

        try:
            self.store.commit([
                make_update(
                    self._name(client_id, period),
                    changes,
                    version,
                )
            ])
        except FirestoreConflict as exc:
            raise ReportConflict(
                "Case changed before consent was saved."
            ) from exc

    def publish(self, client_id, period):
        self.reports._can_write(client_id)

        case, version = self.get(
            client_id,
            period,
        )

        if (
            not version
            or not case.get("draft_hash")
            or case.get("consent_hash")
            != case["draft_hash"]
        ):
            raise WorkspaceReadOnly(
                "Specific case-study consent is missing or outdated."
            )

        profile, profile_version = (
            self.store.get_user_with_version()
        )
        rights = plan_entitlements(
            profile,
            settings,
        )

        if not rights["active"]:
            raise WorkspaceReadOnly(
                "An active plan or trial is required."
            )

        changes = {
            "state": "published",
            "published_at": now(),
        }

        writes = [
            make_update(
                self._name(client_id, period),
                changes,
                version,
            )
        ]

        if rights["plan"] == "solo":
            catalog = self._catalog()
            winner = self._solo_retained(
                catalog,
                profile,
            )

            if (
                winner is not None
                and winner != (client_id, period)
            ):
                visible = any(
                    (
                        item["client_id"],
                        item["period"],
                    ) == winner
                    and item.get("state") == "published"
                    and item.get("consent_hash")
                    == item.get("draft_hash")
                    for item in catalog
                )

                if visible:
                    raise WorkspaceReadOnly(
                        "Solo allows one published case study "
                        "at a time. Unpublish the current one first."
                    )

            # The owner selection is changed with the case record in
            # the same atomic Firestore commit.
            writes.append(
                make_update(
                    self.store.user_name,
                    {
                        "last_case_publish_at": (
                            changes["published_at"]
                        ),
                        "retained_case_client_id": client_id,
                        "retained_case_period": period,
                    },
                    profile_version,
                )
            )

        try:
            self.store.commit(writes)
        except FirestoreConflict as exc:
            raise ReportConflict(
                "Case changed while publishing."
            ) from exc

    def unpublish(self, client_id, period):
        case, version = self.get(
            client_id,
            period,
        )

        if not version:
            raise FirestoreNotFound(
                "Case not found"
            )

        self.store.commit([
            make_update(
                self._name(client_id, period),
                {
                    "state": "draft",
                    "updated_at": now(),
                },
                version,
            )
        ])

    def public(self, client_id, period):
        case, _ = self.get(
            client_id,
            period,
        )

        # Every public HTML/PDF request checks current entitlements.
        if not self.visibility(
            client_id,
            period,
            case=case,
        )["public"]:
            raise FirestoreNotFound(
                "Case unavailable"
            )

        keys = (
            "headline",
            "context",
            "approach",
            "outcome",
            "period",
            "sample_size",
            "median_post_impressions",
            "limitation",
            "trial_only",
        )

        return {
            key: case.get(key)
            for key in keys
        }
