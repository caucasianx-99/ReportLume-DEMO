"""Tenant-scoped monthly reads with bounded, source-aware observation lookups.

Read post documents once using the indexed month query. Consider completed
imports in descending measurement-date/completion order; request observation
documents only for posts that do not already have a better qualified source.

Legacy observation IDs are read, never written. Incomplete imports never
contribute evidence, even when their observations remain in Firestore.
"""

import re

from datetime import date, datetime, timezone

from app.core.firestore_rest import (
    FirestoreUnavailable,
    decode_fields,
)

from app.services.observation_identity import (
    UPLOAD_ID_RE,
    legacy_observation_id,
    observation_id,
)


POST_ID_RE = re.compile(r"^[0-9]{1,128}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MAX_UPLOADS = 5000
MAX_LOOKUPS = 20000
BATCH_SIZE = 100


def _utc_timestamp(raw):
    if not isinstance(raw, str) or not raw:
        return datetime.min.replace(
            tzinfo=timezone.utc
        )

    try:
        parsed = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )

    except ValueError:
        return datetime.min.replace(
            tzinfo=timezone.utc
        )

    if parsed.tzinfo is None:
        return datetime.min.replace(
            tzinfo=timezone.utc
        )

    return parsed.astimezone(timezone.utc)


def _measurement_date(fields, key):
    """Recover historic date-only metadata from its documented key format."""

    candidate = fields.get("measurement_date")

    if not candidate and isinstance(key, str):
        if key.startswith("date_only:"):
            candidate = key[
                len("date_only:"):
            ][:10]

        elif key.startswith("minute:"):
            candidate = key[
                len("minute:"):
            ][:10]

    if (
        not isinstance(candidate, str)
        or not DATE_RE.fullmatch(candidate)
    ):
        return None

    try:
        date.fromisoformat(candidate)

    except ValueError:
        return None

    return candidate


def _completed_uploads(
    store,
    client_id,
    start,
    end,
):
    collection = (
        store.client_name(client_id)
        + "/uploads"
    )

    uploads = []
    visited = set()
    token = ""

    while True:
        response = store.list_collection(
            collection,
            page_size=50,
            page_token=token,
            order_by="created_at desc",
        )

        for document in response.get(
            "documents", []
        ):
            name = document.get(
                "name", ""
            )

            if (
                not isinstance(name, str)
                or not name.startswith(
                    collection + "/"
                )
            ):
                raise FirestoreUnavailable(
                    "Invalid upload record"
                )

            upload_id = name.rsplit(
                "/", 1
            )[-1]

            if not UPLOAD_ID_RE.fullmatch(
                upload_id
            ):
                raise FirestoreUnavailable(
                    "Invalid upload ID"
                )

            fields = decode_fields(
                document.get(
                    "fields", {}
                )
            )

            if (
                fields.get(
                    "owner_uid",
                    store.uid,
                ) != store.uid
            ):
                raise FirestoreUnavailable(
                    "Upload owner mismatch"
                )

            if fields.get("status") != "completed":
                continue

            first = fields.get(
                "published_start"
            )

            last = fields.get(
                "published_end"
            )

            if (
                isinstance(first, str)
                and isinstance(last, str)
            ):
                if last < start or first > end:
                    continue

            key = fields.get(
                "measurement_key"
            )

            if (
                not isinstance(key, str)
                or not key
                or len(key) > 256
            ):
                raise FirestoreUnavailable(
                    "Completed upload has no measurement key"
                )

            uploads.append({
                "id": upload_id,
                "key": key,
                "measurement_date": _measurement_date(
                    fields,
                    key,
                ),
                "completed_at": _utc_timestamp(
                    fields.get("completed_at")
                    or fields.get("created_at")
                ),
                # Firestore updates the upload document when it becomes
                # completed. This server timestamp breaks ties when the
                # application's completed_at clock has equal precision.
                "committed_at": _utc_timestamp(
                    document.get("updateTime")
                ),
                "commit_token": (
                    document.get("updateTime")
                    if isinstance(document.get("updateTime"), str)
                    else ""
                ),
            })

            if len(uploads) > MAX_UPLOADS:
                raise FirestoreUnavailable(
                    "Too many completed uploads"
                )

        next_token = response.get(
            "nextPageToken", ""
        )

        if not next_token:
            break

        if (
            not isinstance(next_token, str)
            or next_token in visited
            or next_token == token
        ):
            raise FirestoreUnavailable(
                "Upload pagination did not advance"
            )

        visited.add(token)
        token = next_token

    # Historic uploads with unknown dates still need inspection.
    uploads.sort(
        key=lambda item: _source_rank(item),
        reverse=True,
    )

    return uploads


def _source_rank(upload, measurement_date=None):
    return (
        (
            upload["measurement_date"]
            if measurement_date is None
            else measurement_date
        ) or "",
        upload["completed_at"],
        upload["committed_at"],
        upload["commit_token"],
        upload["id"],
    )


def _rank(observation, upload):
    return _source_rank(
        upload,
        observation.get("measurement_date") or "",
    )


def _check_source(
    document,
    name,
    post_id,
    upload,
    store,
):
    if document.get("name") != name:
        raise FirestoreUnavailable(
            "Observation name mismatch"
        )

    fields = decode_fields(
        document.get(
            "fields", {}
        )
    )

    if (
        fields.get(
            "owner_uid",
            store.uid,
        ) != store.uid
        or fields.get("source") != "x_analytics"
        or fields.get("post_id") != post_id
    ):
        raise FirestoreUnavailable(
            "Observation failed provenance validation"
        )

    # A legacy document name can be shared by
    # multiple CSV imports having the same key.
    # Ignore an unrelated import instead of
    # allowing it to invalidate a completed one.

    if fields.get(
        "upload_id"
    ) != upload["id"]:
        return None

    if fields.get(
        "measurement_key"
    ) != upload["key"]:
        raise FirestoreUnavailable(
            "Observation measurement key mismatch"
        )

    actual = fields.get(
        "measurement_date"
    )

    if (
        not isinstance(actual, str)
        or not DATE_RE.fullmatch(actual)
    ):
        raise FirestoreUnavailable(
            "Observation measurement date invalid"
        )

    if (
        upload["measurement_date"] is not None
        and actual != upload["measurement_date"]
    ):
        raise FirestoreUnavailable(
            "Upload observation date mismatch"
        )

    return fields


def load_month_posts(
    store,
    client_id,
    start: date,
    end: date,
) -> list:
    """Load one eligible observation for each post.

    Newer completed sources are examined first. The reader retains a
    bounded document lookup budget rather than requesting every
    post/upload combination unconditionally.
    """

    if (
        not isinstance(start, date)
        or not isinstance(end, date)
        or start > end
    ):
        raise ValueError(
            "Invalid month bounds"
        )

    start_text = start.isoformat()
    end_text = end.isoformat()

    client_path = store.client_name(
        client_id
    )

    documents = store.query_posts_in_period(
        client_id,
        start_text,
        end_text,
    )

    if not documents:
        return []

    posts = {}

    for document in documents:
        name = document.get(
            "name", ""
        )

        prefix = (
            client_path
            + "/posts/"
        )

        if (
            not isinstance(name, str)
            or not name.startswith(prefix)
        ):
            raise FirestoreUnavailable(
                "Invalid post path"
            )

        if (
            len(name.split("/"))
            != len(
                prefix.rstrip("/").split("/")
            ) + 1
        ):
            raise FirestoreUnavailable(
                "Unexpected nested post"
            )

        post_id = name.rsplit(
            "/", 1
        )[-1]

        if (
            not POST_ID_RE.fullmatch(post_id)
            or post_id in posts
        ):
            raise FirestoreUnavailable(
                "Duplicate or invalid Post ID"
            )

        fields = decode_fields(
            document.get("fields", {})
        )

        if (
            fields.get(
                "owner_uid",
                store.uid,
            ) != store.uid
        ):
            raise FirestoreUnavailable(
                "Post owner mismatch"
            )

        published = fields.get(
            "published_date"
        )

        if (
            not isinstance(published, str)
            or not start_text <= published <= end_text
        ):
            raise FirestoreUnavailable(
                "Post outside reporting window"
            )

        posts[post_id] = {
            "post_id": post_id,
            "published_date": published,
            "authorship": fields.get(
                "authorship",
                "Unknown",
            ),
            "content_type": fields.get(
                "content_type",
                "Unknown",
            ),
            "fields": fields,
        }

    uploads = _completed_uploads(
        store,
        client_id,
        start_text,
        end_text,
    )

    if not uploads:
        return []

    selected = {}
    selected_rank = {}
    reads = 0

    for upload in uploads:
        # Skip a post only when the current
        # completed observation has a better
        # known source rank.

        proposed = _source_rank(upload)

        unresolved = [
            post_id
            for post_id in posts
            if (
                post_id not in selected
                or upload["measurement_date"] is None
                or proposed > selected_rank[post_id]
            )
        ]

        if not unresolved:
            continue

        new_id = observation_id(
            upload["key"],
            upload["id"],
        )

        old_id = legacy_observation_id(
            upload["key"]
        )

        requests = {}

        for post_id in unresolved:
            base = (
                f"{client_path}/posts/"
                f"{post_id}/observations/"
            )

            requests[
                base + new_id
            ] = post_id

            requests[
                base + old_id
            ] = post_id

        names = list(requests)

        reads += len(names)

        if reads > MAX_LOOKUPS:
            raise FirestoreUnavailable(
                "Monthly observation lookup budget exceeded"
            )

        for index in range(
            0,
            len(names),
            BATCH_SIZE,
        ):
            chunk = names[
                index:index + BATCH_SIZE
            ]

            found = store.batch_get_documents(
                chunk
            )

            if set(found) != set(chunk):
                raise FirestoreUnavailable(
                    "Incomplete observation lookup"
                )

            for name in chunk:
                document = found[name]

                if document is None:
                    continue

                post_id = requests[name]

                observation = _check_source(
                    document,
                    name,
                    post_id,
                    upload,
                    store,
                )

                if observation is None:
                    continue

                rank = _rank(
                    observation,
                    upload,
                )

                if (
                    post_id not in selected
                    or rank > selected_rank[post_id]
                ):
                    selected[post_id] = observation
                    selected_rank[post_id] = rank

    return [
        {
            **row,
            "observation": selected[post_id],
        }
        for post_id, row in posts.items()
        if post_id in selected
    ]
