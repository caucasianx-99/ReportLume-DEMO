
"""Manual classification of imported X Content posts.

Post type, authorship, topic and format are independent.
Only posts connected to completed imports are displayed.
"""

import base64
import re

from datetime import datetime, timezone

from app.core.firestore_rest import (
    FirestoreConflict,
    FirestoreNotFound,
    FirestoreUnavailable,
    decode_fields,
    encode_fields,
)
from app.services.proof_of_work import month_bounds, ProofError

from app.services.client_workspace import (
    WorkspaceReadOnly,
    has_paid_access,
    require_write_access,
)


POST_ID_RE = re.compile(r"^[0-9]{1,128}$")
UPLOAD_ID_RE = re.compile(r"^[a-f0-9]{64}$")

MAX_BATCH = 20
PAGE_SIZE = 100
QUERY_SIZE = 100
MAX_MONTH_SCANNED = 400

AUTHORSHIP = (
    "Ghostwriter",
    "Client",
    "Other",
    "Unknown",
)

CONTENT_TYPES = (
    "Post",
    "Reply",
    "Unknown",
)

FORMATS = (
    "Educational",
    "Opinion",
    "Personal Story",
    "Case Study",
    "Reply",
    "Other",
    "Unknown",
)

DEFAULTS = {
    "authorship": "Unknown",
    "content_type": "Unknown",
    "topic": "",
    "format": "Unknown",
}


class ClassificationError(ValueError):
    pass


class ClassificationConflict(RuntimeError):
    pass


def normalize_changes(changes):
    if not isinstance(changes, dict) or not changes:
        raise ClassificationError(
            "At least one classification field is required."
        )

    if not set(changes).issubset(DEFAULTS):
        raise ClassificationError(
            "Unsupported classification field."
        )

    result = {}

    for field, value in changes.items():
        if not isinstance(value, str):
            raise ClassificationError(
                f"{field} must be text."
            )

        value = value.strip()

        if field == "authorship" and value not in AUTHORSHIP:
            raise ClassificationError(
                "Invalid authorship."
            )

        if (
            field == "content_type"
            and value not in CONTENT_TYPES
        ):
            raise ClassificationError(
                "Invalid content type."
            )

        if field == "format" and value not in FORMATS:
            raise ClassificationError(
                "Invalid content format."
            )

        if field == "topic":
            if (
                len(value) > 80
                or any(ord(c) < 32 for c in value)
            ):
                raise ClassificationError(
                    "Topic must be at most 80 characters."
                )

        result[field] = value

    return result


class PostClassificationService:

    def __init__(self, store):
        self.store = store

    def _upload_completed(
        self,
        client_id,
        upload_id,
        cache,
    ):
        if not UPLOAD_ID_RE.fullmatch(
            upload_id or ""
        ):
            return False

        if upload_id in cache:
            return cache[upload_id]

        name = (
            self.store.client_name(client_id)
            + "/uploads/"
            + upload_id
        )

        try:
            document = self.store.get_document(name)

            fields = decode_fields(
                document.get("fields", {})
            )

            completed = (
                fields.get("status") == "completed"
            )

        except FirestoreNotFound:
            completed = False

        cache[upload_id] = completed
        return completed

    def _has_completed_source(
        self,
        client_id,
        post_id,
        post,
        cache,
    ):
        latest = post.get("last_seen_upload", "")

        if self._upload_completed(
            client_id,
            latest,
            cache,
        ):
            return True

        # A failed recent import may have overwritten
        # last_seen_upload. Earlier completed observations
        # must remain accessible.
        collection = (
            self.store.client_name(client_id)
            + "/posts/"
            + post_id
            + "/observations"
        )

        token = ""

        for _ in range(10):
            response = self.store.list_collection(
                collection,
                page_size=50,
                page_token=token,
            )

            for document in response.get(
                "documents", []
            ):
                observation = decode_fields(
                    document.get("fields", {})
                )

                if self._upload_completed(
                    client_id,
                    observation.get("upload_id", ""),
                    cache,
                ):
                    return True

            token = response.get("nextPageToken", "")

            if not token:
                break

        return False

    @staticmethod
    def _encode_cursor(published, post_id):
        raw = f"{published}|{post_id}".encode("ascii")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(token, start, end):
        if not isinstance(token, str) or len(token) > 256:
            raise ClassificationError(
                "Invalid classification cursor."
            )

        if not token:
            return None

        try:
            raw = base64.b64decode(
                token + ("=" * (-len(token) % 4)),
                altchars=b"-_",
                validate=True,
            ).decode("ascii")

            published, post_id = raw.split("|", 1)

        except (ValueError, UnicodeError) as exc:
            raise ClassificationError(
                "Invalid classification cursor."
            ) from exc

        if (
            not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published)
            or not start <= published <= end
            or not POST_ID_RE.fullmatch(post_id)
        ):
            raise ClassificationError(
                "Invalid classification cursor."
            )

        if (
            PostClassificationService._encode_cursor(
                published,
                post_id,
            )
            != token
        ):
            raise ClassificationError(
                "Invalid classification cursor."
            )

        return published, post_id

    @staticmethod
    def _display_date(value):
        """Present the stored ISO calendar date in US English."""
        try:
            parsed = datetime.strptime(
                value,
                "%Y-%m-%d",
            )
        except (TypeError, ValueError):
            return "Date unavailable"

        return (
            f"{parsed.strftime('%B')} "
            f"{parsed.day}, {parsed.year}"
        )

    def _row(self, document):
        name = document.get("name", "")
        post_id = name.rsplit("/", 1)[-1]

        if not POST_ID_RE.fullmatch(post_id):
            return None

        fields = decode_fields(
            document.get("fields", {})
        )

        if (
            fields.get("owner_uid", self.store.uid)
            != self.store.uid
        ):
            raise FirestoreUnavailable(
                "Post owner mismatch."
            )

        version = document.get("updateTime")

        if not isinstance(version, str):
            raise FirestoreUnavailable(
                "Post version unavailable."
            )

        row = {
            "post_id": post_id,
            "published_date": fields.get("published_date", ""),
            "post_text": fields.get("post_text", ""),
            "display_date": self._display_date(
                fields.get("published_date", "")
            ),
            "version": version,
        }

        for key, default in DEFAULTS.items():
            row[key] = fields.get(key, default)

        row["possible_reply"] = (
            str(row["post_text"]).lstrip().startswith("@")
            and row["content_type"] == "Unknown"
        )

        return row, fields

    def _legacy_page(self, client_id, page_token):
        """Keep legacy unfiltered pagination for existing links and tests."""
        collection = (
            self.store.client_name(client_id)
            + "/posts"
        )

        response = self.store.list_collection(
            collection,
            page_size=min(PAGE_SIZE, 50),
            page_token=page_token,
        )

        completed_cache = {}
        visible = []
        excluded = 0

        for document in response.get("documents", []):
            parsed = self._row(document)

            if parsed is None:
                continue

            row, fields = parsed

            if not self._has_completed_source(
                client_id,
                row["post_id"],
                fields,
                completed_cache,
            ):
                excluded += 1
                continue

            visible.append(row)

        return {
            "posts": visible,
            "next_page_token": response.get(
                "nextPageToken",
                "",
            ),
            "excluded_processing": excluded,
        }

    def _monthly_page(
        self,
        client_id,
        page_token,
        period,
    ):
        """Seek through one month, newest first, without scanning all posts."""
        try:
            start, end = month_bounds(period)

        except (ProofError, ValueError, TypeError) as exc:
            raise ClassificationError(
                "Invalid reporting month."
            ) from exc

        begin = start.isoformat()
        finish = end.isoformat()

        cursor = self._decode_cursor(
            page_token,
            begin,
            finish,
        )

        parent = self.store.client_name(client_id)
        collection = parent + "/posts/"

        completed_cache = {}
        visible = []
        excluded = 0
        scanned = 0
        last_cursor = cursor
        more = False

        while (
            scanned < MAX_MONTH_SCANNED
            and len(visible) < PAGE_SIZE
        ):
            query = {
                "from": [
                    {
                        "collectionId": "posts",
                    }
                ],
                "where": {
                    "compositeFilter": {
                        "op": "AND",
                        "filters": [
                            {
                                "fieldFilter": {
                                    "field": {
                                        "fieldPath": "published_date",
                                    },
                                    "op": "GREATER_THAN_OR_EQUAL",
                                    "value": {
                                        "stringValue": begin,
                                    },
                                }
                            },
                            {
                                "fieldFilter": {
                                    "field": {
                                        "fieldPath": "published_date",
                                    },
                                    "op": "LESS_THAN_OR_EQUAL",
                                    "value": {
                                        "stringValue": finish,
                                    },
                                }
                            },
                        ],
                    },
                },
                "orderBy": [
                    {
                        "field": {
                            "fieldPath": "published_date",
                        },
                        "direction": "DESCENDING",
                    },
                    {
                        "field": {
                            "fieldPath": "__name__",
                        },
                        "direction": "DESCENDING",
                    },
                ],
                "limit": QUERY_SIZE,
            }

            if last_cursor is not None:
                query["startAt"] = {
                    "values": [
                        {
                            "stringValue": last_cursor[0],
                        },
                        {
                            "referenceValue": (
                                collection
                                + last_cursor[1]
                            ),
                        },
                    ],
                    "before": False,
                }

            stream = self.store.run_query(
                parent,
                query,
            )

            documents = [
                item["document"]
                for item in stream
                if "document" in item
            ]

            if len(documents) > QUERY_SIZE:
                raise FirestoreUnavailable(
                    "Classification page exceeded its limit."
                )

            if not documents:
                more = False
                break

            for index, document in enumerate(documents):
                name = document.get("name", "")

                if (
                    not isinstance(name, str)
                    or not name.startswith(collection)
                ):
                    raise FirestoreUnavailable(
                        "Unexpected classification post path."
                    )

                if (
                    len(name.split("/"))
                    != len(parent.split("/")) + 2
                ):
                    raise FirestoreUnavailable(
                        "Nested post is not allowed."
                    )

                post_id = name.rsplit("/", 1)[-1]
                parsed = self._row(document)

                if parsed is None:
                    raise FirestoreUnavailable(
                        "Invalid classification Post ID."
                    )

                row, fields = parsed
                published = row["published_date"]

                if (
                    not isinstance(published, str)
                    or not begin <= published <= finish
                ):
                    raise FirestoreUnavailable(
                        "Post outside selected month."
                    )

                current_cursor = (
                    published,
                    post_id,
                )

                if (
                    last_cursor is not None
                    and current_cursor >= last_cursor
                ):
                    raise FirestoreUnavailable(
                        "Classification cursor did not advance."
                    )

                last_cursor = current_cursor
                scanned += 1

                if not self._has_completed_source(
                    client_id,
                    post_id,
                    fields,
                    completed_cache,
                ):
                    excluded += 1
                else:
                    visible.append(row)

                if len(visible) >= PAGE_SIZE:
                    more = (
                        index + 1 < len(documents)
                        or len(documents) == QUERY_SIZE
                    )
                    break

                if scanned >= MAX_MONTH_SCANNED:
                    more = True
                    break

            if (
                len(visible) >= PAGE_SIZE
                or scanned >= MAX_MONTH_SCANNED
            ):
                break

            if len(documents) < QUERY_SIZE:
                more = False
                break

            more = True

        return {
            "posts": visible,
            "next_page_token": (
                self._encode_cursor(*last_cursor)
                if more and last_cursor is not None
                else ""
            ),
            "excluded_processing": excluded,
        }

    def list_posts(
        self,
        client_id,
        page_token="",
        period="",
    ):
        """Use period-scoped cursor pagination, or legacy page tokens."""
        self.store.get_client(client_id)

        if period:
            return self._monthly_page(
                client_id,
                page_token,
                period,
            )

        return self._legacy_page(
            client_id,
            page_token,
        )

    def _require_editable(self, client_id):
        profile, _ = (
            self.store.get_user_with_version()
        )

        require_write_access(profile)
        client = self.store.get_client(client_id)

        if client.get("status") != "active":
            raise WorkspaceReadOnly(
                "Only active clients can be edited."
            )

        if (
            not has_paid_access(profile)
            and profile.get("unpaid_client_id") != client_id
        ):
            raise WorkspaceReadOnly(
                "This client is not covered by your current access."
            )

    def apply_patches(
        self,
        client_id,
        patches,
    ):
        if (
            not isinstance(patches, list)
            or not 1 <= len(patches) <= MAX_BATCH
        ):
            raise ClassificationError(
                "Select between 1 and 20 posts."
            )

        self._require_editable(client_id)

        seen = set()
        writes = []
        changes_applied = []
        completed_cache = {}

        now = datetime.now(
            timezone.utc
        ).isoformat()

        for patch in patches:
            if not isinstance(patch, dict):
                raise ClassificationError(
                    "Invalid post update."
                )

            post_id = patch.get("post_id")
            version = patch.get("expected_version")

            if (
                not isinstance(post_id, str)
                or not POST_ID_RE.fullmatch(post_id)
                or post_id in seen
            ):
                raise ClassificationError(
                    "Invalid or duplicated Post ID."
                )

            if (
                not isinstance(version, str)
                or not 15 <= len(version) <= 64
            ):
                raise ClassificationError(
                    "Missing or invalid document version."
                )

            seen.add(post_id)

            normalized = normalize_changes(
                patch.get("changes")
            )

            name = (
                self.store.client_name(client_id)
                + "/posts/"
                + post_id
            )

            try:
                document = self.store.get_document(name)

            except FirestoreNotFound as exc:
                raise ClassificationConflict(
                    "Post no longer exists. Refresh the page."
                ) from exc

            if document["updateTime"] != version:
                raise ClassificationConflict(
                    "A post changed in another session. "
                    "Refresh before saving."
                )

            fields = decode_fields(
                document.get("fields", {})
            )

            if not self._has_completed_source(
                client_id,
                post_id,
                fields,
                completed_cache,
            ):
                raise ClassificationError(
                    "This post does not belong to a completed import."
                )

            previous = {
                field: fields.get(
                    field,
                    DEFAULTS[field],
                )
                for field in normalized
            }

            new_values = {
                **normalized,
                "classification_source": (
                    "ghostwriter_input"
                ),
                "classification_updated_at": now,
            }

            writes.append({
                "update": {
                    "name": name,
                    "fields": encode_fields(
                        new_values
                    ),
                },
                "updateMask": {
                    "fieldPaths": list(
                        new_values.keys()
                    ),
                },
                "currentDocument": {
                    "updateTime": version,
                },
            })

            changes_applied.append({
                "post_id": post_id,
                "before": previous,
                "after": normalized,
            })

        try:
            result = self.store.commit(writes)

        except FirestoreConflict as exc:
            raise ClassificationConflict(
                "Another update happened before saving. "
                "Refresh the page."
            ) from exc

        write_results = result.get(
            "writeResults",
            [],
        )

        if len(write_results) != len(writes):
            raise FirestoreUnavailable(
                "Could not verify saved document versions."
            )

        for index, item in enumerate(changes_applied):
            new_version = write_results[index].get(
                "updateTime"
            )

            if not new_version:
                raise FirestoreUnavailable(
                    "Missing saved document version."
                )

            item["version"] = new_version

        return changes_applied
