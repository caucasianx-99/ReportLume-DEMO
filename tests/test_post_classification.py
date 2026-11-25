
"""Classification service tests using fake Firestore data."""

import pytest

from app.core.firestore_rest import (
    FirestoreNotFound,
    encode_fields,
)
from app.services.client_workspace import WorkspaceReadOnly
from app.services.post_classification import (
    ClassificationConflict,
    ClassificationError,
    PostClassificationService,
    normalize_changes,
)


CLIENT_ID = "a" * 32
POST_ID = "12345678901234567890"
UPLOAD_ID = "b" * 64

VERSION = "2026-11-03T10:00:00Z"
NEW_VERSION = "2026-11-03T10:01:00Z"


class FakeStore:
    uid = "verified_user"

    user_name = (
        "projects/test/databases/(default)"
        "/documents/users/verified_user"
    )

    def __init__(
        self,
        completed=True,
        status="active",
    ):
        self.completed = completed
        self.status = status
        self.writes = []

        self.profile = {
            "unpaid_client_id": CLIENT_ID,
        }

    def client_name(self, client_id):
        return (
            self.user_name
            + "/clients/"
            + client_id
        )

    def get_client(self, client_id):
        if client_id != CLIENT_ID:
            raise FirestoreNotFound("Missing client")

        return {"status": self.status}

    def get_user_with_version(self):
        return dict(self.profile), VERSION

    def post_document(self):
        return {
            "name": (
                self.client_name(CLIENT_ID)
                + "/posts/"
                + POST_ID
            ),
            "updateTime": VERSION,
            "fields": encode_fields({
                "post_id": POST_ID,
                "post_text": "@someone Example post",
                "published_date": "2026-10-29",
                "last_seen_upload": UPLOAD_ID,
                "impressions": 100,
            }),
        }

    def get_document(self, name):
        if name.endswith("/posts/" + POST_ID):
            return self.post_document()

        if name.endswith("/uploads/" + UPLOAD_ID):
            return {
                "name": name,
                "fields": encode_fields({
                    "status": (
                        "completed"
                        if self.completed
                        else "processing"
                    )
                }),
            }

        raise FirestoreNotFound("Missing document")

    def list_collection(
        self,
        resource,
        page_size=20,
        page_token="",
        order_by="",
    ):
        if resource.endswith("/posts"):
            return {
                "documents": [
                    self.post_document()
                ],
            }

        return {"documents": []}

    def commit(self, writes):
        self.writes.append(writes)

        return {
            "writeResults": [
                {"updateTime": NEW_VERSION}
                for _ in writes
            ]
        }


def test_unclassified_post_defaults_unknown():
    service = PostClassificationService(
        FakeStore()
    )

    result = service.list_posts(CLIENT_ID)

    assert len(result["posts"]) == 1

    post = result["posts"][0]

    assert post["authorship"] == "Unknown"
    assert post["content_type"] == "Unknown"
    assert post["topic"] == ""
    assert post["format"] == "Unknown"

    assert post["possible_reply"] is True


def test_incomplete_import_not_shown():
    service = PostClassificationService(
        FakeStore(completed=False)
    )

    result = service.list_posts(CLIENT_ID)

    assert result["posts"] == []
    assert result["excluded_processing"] == 1


def test_authorship_and_type_independent():
    store = FakeStore()

    service = PostClassificationService(store)

    updated = service.apply_patches(
        CLIENT_ID,
        [{
            "post_id": POST_ID,
            "expected_version": VERSION,
            "changes": {
                "authorship": "Ghostwriter",
                "content_type": "Reply",
            },
        }],
    )

    assert updated[0]["before"] == {
        "authorship": "Unknown",
        "content_type": "Unknown",
    }

    assert updated[0]["version"] == NEW_VERSION

    write = store.writes[0][0]

    fields = write["update"]["fields"]

    assert fields["authorship"] == {
        "stringValue": "Ghostwriter"
    }

    assert fields["content_type"] == {
        "stringValue": "Reply"
    }

    # Existing analytics data is not overwritten.
    assert "impressions" not in (
        write["updateMask"]["fieldPaths"]
    )

    assert "post_text" not in (
        write["updateMask"]["fieldPaths"]
    )


def test_topic_and_format_are_manual():
    store = FakeStore()

    service = PostClassificationService(store)

    service.apply_patches(
        CLIENT_ID,
        [{
            "post_id": POST_ID,
            "expected_version": VERSION,
            "changes": {
                "topic": "Product education",
                "format": "Educational",
            },
        }],
    )

    fields = store.writes[0][0][
        "update"
    ]["fields"]

    assert fields["topic"] == {
        "stringValue": "Product education"
    }

    assert fields["format"] == {
        "stringValue": "Educational"
    }

    assert "authorship" not in fields


def test_invalid_author_rejected():
    with pytest.raises(ClassificationError):
        normalize_changes({
            "authorship": "Automatically inferred"
        })


def test_unapproved_field_rejected():
    with pytest.raises(ClassificationError):
        normalize_changes({
            "impressions": "999999"
        })


def test_duplicate_post_rejected():
    service = PostClassificationService(
        FakeStore()
    )

    patch = {
        "post_id": POST_ID,
        "expected_version": VERSION,
        "changes": {
            "authorship": "Client",
        },
    }

    with pytest.raises(ClassificationError):
        service.apply_patches(
            CLIENT_ID,
            [patch, patch],
        )


def test_stale_version_rejected():
    service = PostClassificationService(
        FakeStore()
    )

    with pytest.raises(ClassificationConflict):
        service.apply_patches(
            CLIENT_ID,
            [{
                "post_id": POST_ID,
                "expected_version": (
                    "2026-11-03T09:00:00Z"
                ),
                "changes": {
                    "authorship": "Client",
                },
            }],
        )


def test_archived_client_is_read_only():
    service = PostClassificationService(
        FakeStore(status="archived")
    )

    with pytest.raises(WorkspaceReadOnly):
        service.apply_patches(
            CLIENT_ID,
            [{
                "post_id": POST_ID,
                "expected_version": VERSION,
                "changes": {
                    "authorship": "Ghostwriter",
                },
            }],
        )


def test_processing_import_cannot_be_classified():
    service = PostClassificationService(
        FakeStore(completed=False)
    )

    with pytest.raises(ClassificationError):
        service.apply_patches(
            CLIENT_ID,
            [{
                "post_id": POST_ID,
                "expected_version": VERSION,
                "changes": {
                    "content_type": "Post",
                },
            }],
        )
