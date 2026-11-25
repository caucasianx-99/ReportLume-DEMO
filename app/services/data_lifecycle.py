"""Client data export and gated client deletion.

The export streams every document under the client, in name order, from a
single paginated all-descendants query, so it scales with the data instead
of stopping at a fixed document count. It ends with ``"complete": true``; a
download without that line was interrupted and must not be relied on.

Self-service deletion is disabled by default until live Firestore tests
establish correct nested collection enumeration, atomic quota finalization
and recovery after partial failures. Support deletions use ``purge_client``
and ``purge_account`` (through ``app.scripts.delete_account``): they delete
with the same all-descendants query as the export, have no document cap and
can be run again after an interruption. Never treat a deletion request as
done when the backend cannot prove that its writes completed.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.firestore_rest import (
    SAFE_ID, FirestoreConflict, FirestoreNotFound,
    FirestoreUnavailable, decode_fields,
)
from app.services.client_workspace import make_update, now_iso

MAX_CLIENT_DOCUMENTS = 1000
MAX_COLLECTION_PAGES = 100
MAX_EXPORT_DOCUMENTS = 200_000
EXPORT_PAGE_SIZE = 300
DELETE_BATCH = 20  # TenantStore.commit accepts at most 20 writes

# Billing states in which the payment provider may still charge the owner.
CHARGEABLE_STATES = frozenset({"active", "past_due", "unpaid", "paused"})
# After a checkout link stops being payable, wait this long for a late
# provider event before the account (and the checkout intent) is deleted.
CHECKOUT_SETTLEMENT_GRACE = timedelta(hours=48)
MAX_EMAIL_CLAIMS = 1000
_API = "https://firestore.googleapis.com/v1/"


class DataLimit(RuntimeError):
    pass


class DataDeletionDisabled(RuntimeError):
    pass


class AccountDeletionRefused(RuntimeError):
    pass


def _collection_ids(store, document_path):
    store._check_resource(document_path)
    next_page = ""
    result = []
    for _ in range(MAX_COLLECTION_PAGES):
        payload = {"pageSize": 100}
        if next_page:
            payload["pageToken"] = next_page
        response = store._request(
            "POST",
            "https://firestore.googleapis.com/v1/" + document_path + ":listCollectionIds",
            json=payload,
        )
        for collection_id in response.get("collectionIds", []):
            if not SAFE_ID.fullmatch(collection_id):
                raise FirestoreUnavailable("Unrecognized collection identifier.")
            result.append(collection_id)
        next_page = response.get("nextPageToken", "")
        if not next_page:
            return result
    raise DataLimit("Collection enumeration exceeded safety limits.")


def _children(store, collection_path):
    next_page = ""
    for _ in range(MAX_COLLECTION_PAGES):
        response = store.list_collection(
            collection_path, page_size=50, page_token=next_page,
        )
        for document in response.get("documents", []):
            name = document.get("name", "")
            store._check_resource(name)
            yield document
        next_page = response.get("nextPageToken", "")
        if not next_page:
            return
    raise DataLimit("Document pagination exceeded safety limits.")


def enumerate_client_tree(store, client_id):
    """Collect complete visible tree before any destructive write."""
    root = store.client_name(client_id)
    records = []

    def visit(document_path, parent=None):
        if len(records) >= MAX_CLIENT_DOCUMENTS:
            raise DataLimit("Client exceeds safe interactive deletion size.")
        document = store.get_document(document_path) if parent is None else parent
        result = {"path": document_path, "fields": decode_fields(document.get("fields", {}))}
        for collection in _collection_ids(store, document_path):
            for child in _children(store, document_path + "/" + collection):
                visit(child["name"], child)
        records.append(result)  # postorder for deletion

    visit(root)
    return records


def _path_key(name: str) -> tuple:
    """Firestore orders document names segment by segment."""
    return tuple(name.split("/"))


def descendant_documents(store, root: str):
    """Yield every document below ``root`` in name order, page by page."""
    store._check_resource(root)
    query = {
        "from": [{"allDescendants": True}],
        "orderBy": [{"field": {"fieldPath": "__name__"}, "direction": "ASCENDING"}],
        "limit": EXPORT_PAGE_SIZE,
    }
    previous = None
    count = 0

    while True:
        if previous is not None:
            query["startAt"] = {
                "values": [{"referenceValue": previous}],
                "before": False,
            }

        response = store._request(
            "POST", _API + root + ":runQuery",
            json={"structuredQuery": query},
        )

        if not isinstance(response, list):
            raise FirestoreUnavailable("Invalid Firestore query stream.")

        documents = [
            item["document"] for item in response
            if isinstance(item, dict) and isinstance(item.get("document"), dict)
        ]

        if len(documents) > EXPORT_PAGE_SIZE:
            raise FirestoreUnavailable("Firestore page exceeded the requested size.")

        for document in documents:
            name = document.get("name")
            store._check_resource(name)

            if not name.startswith(root + "/"):
                raise FirestoreUnavailable("Query returned a document outside the client.")

            if previous is not None and _path_key(name) <= _path_key(previous):
                raise FirestoreUnavailable("Export pagination did not advance.")

            previous = name
            count += 1

            if count > MAX_EXPORT_DOCUMENTS:
                raise DataLimit("Client export exceeds the safety limit.")

            yield document

        if len(documents) < EXPORT_PAGE_SIZE:
            return


def export_client_chunks(store, client_id):
    """Read the client record now, then stream the export as JSON text.

    A missing client raises FirestoreNotFound before any byte is sent.
    """
    root = store.client_name(client_id)
    root_document = store.get_document(root)

    def chunks():
        yield json.dumps({
            "format": "reportlume-client-export-v2",
            "exported_at": now_iso(),
            "client_id": client_id,
            "note": "Contains normalized records; original source CSV is not retained.",
        }, ensure_ascii=False)[:-1] + ', "documents": ['

        first = True

        for document in itertools.chain(
            [root_document], descendant_documents(store, root),
        ):
            entry = {
                "path": document["name"],
                "fields": decode_fields(document.get("fields", {})),
            }
            yield ("\n" if first else ",\n") + json.dumps(entry, ensure_ascii=False)
            first = False

        yield '\n], "complete": true}\n'

    return chunks()


def export_client_json(store, client_id):
    """The whole export as one string (tests and small scripts)."""
    return "".join(export_client_chunks(store, client_id))


def delete_client(store, client_id):
    """Permanently remove a bounded client subtree, preserving used trial flags.

    This function is unavailable unless DATA_DELETION_ENABLED is true.
    A staged state blocks future client edits and supports retry on failure.
    """
    if not settings.data_deletion_enabled:
        raise DataDeletionDisabled("Data deletion awaits live QA.")

    # Preflight can fail safely before any changes are made.
    records = enumerate_client_tree(store, client_id)
    _mark_client_deleting(store, store.get_client(client_id))
    root = store.client_name(client_id)
    # Re-read after state flag: Firestore documents may have new versions.
    records = enumerate_client_tree(store, client_id)
    names = [entry["path"] for entry in records if entry["path"] != root]
    for offset in range(0, len(names), DELETE_BATCH):
        store.commit([
            {"delete": name} for name in names[offset:offset + DELETE_BATCH]
        ])
    _finish_client_deletion(store, client_id, root)


def _mark_client_deleting(store, client):
    """Archive the client and remember its status so the quota can follow."""
    if client.get("deletion_started_at"):
        return
    store.commit([make_update(store.client_name(client["id"]), {
        "status": "archived",
        "deletion_started_at": now_iso(),
        "deletion_original_status": client.get("status"),
    }, client["_update_time"])])


def _finish_client_deletion(store, client_id, root):
    """Delete the client record and release its active-client slot."""
    for _ in range(3):
        client = store.get_client(client_id)
        profile, version = store.get_user_with_version()
        original = client.get("deletion_original_status")
        decrease = 1 if original in ("active", "paused") else 0
        count = int(profile.get("active_client_count", 0))
        if count < decrease:
            raise FirestoreUnavailable("Client quota requires manual reconciliation.")
        writes = [{
            "delete": root,
            "currentDocument": {"updateTime": client["_update_time"]},
        }]
        if decrease:
            writes.append(make_update(store.user_name, {
                "active_client_count": count - decrease,
            }, version))
        try:
            store.commit(writes)
            return
        except FirestoreConflict:
            continue
    raise FirestoreUnavailable("Client deletion requires retry.")


def _delete_descendants(store, root):
    """Delete every document below ``root``; safe to run again.

    Each pass reads the first page of the remaining tree and deletes it, so
    an interrupted run simply continues where the data stops.
    """
    deleted = 0
    previous_first = None

    while True:
        names = [
            document["name"] for document in itertools.islice(
                descendant_documents(store, root), EXPORT_PAGE_SIZE,
            )
        ]
        if not names:
            return deleted
        if names[0] == previous_first:
            raise FirestoreUnavailable("Deletion did not advance.")
        previous_first = names[0]

        for offset in range(0, len(names), DELETE_BATCH):
            store.commit([
                {"delete": name} for name in names[offset:offset + DELETE_BATCH]
            ])
        deleted += len(names)


def count_client_documents(store, client_id):
    """Documents a client deletion would remove, the client record included."""
    root = store.client_name(client_id)
    try:
        store.get_document(root)
        own = 1
    except FirestoreNotFound:
        own = 0
    return own + sum(1 for _ in descendant_documents(store, root))


def count_account_documents(store):
    """Documents an account deletion would remove: the profile, everything
    under it and the account's checkout e-mail claims."""
    try:
        store.get_document(store.user_name)
        own = 1
    except FirestoreNotFound:
        own = 0
    claims = len(_checkout_email_claims(store, MAX_EMAIL_CLAIMS))
    if claims >= MAX_EMAIL_CLAIMS:
        raise DataLimit("Checkout e-mail claims exceeded safety limits.")
    return own + claims + sum(1 for _ in descendant_documents(store, store.user_name))


def purge_client(store, client_id):
    """Support deletion of one client, with no document cap.

    Not gated by DATA_DELETION_ENABLED: it is run by the operator after a
    verified request. The account keeps its profile, trial and payment
    history; an active client's slot is released.
    """
    root = store.client_name(client_id)
    try:
        client = store.get_client(client_id)
    except FirestoreNotFound:
        client = None  # an earlier run already removed the record

    if client is not None:
        _mark_client_deleting(store, client)

    deleted = _delete_descendants(store, root)

    if client is not None:
        _finish_client_deletion(store, client_id, root)
        deleted += 1
    return deleted


def account_deletion_blocker(store, now=None):
    """Do not operate on a legacy commercial account in this edition."""
    try:
        profile, _ = store.get_user_with_version()
    except FirestoreNotFound:
        return None
    if profile.get("billing_subscription_id") or profile.get("billing_http_checkout_intent_id"):
        return "Legacy commercial account: resolve and export it using the original private deployment first."
    return None


def purge_account(store, now=None):
    reason = account_deletion_blocker(store, now)
    if reason:
        raise AccountDeletionRefused(reason)
    deleted = _delete_descendants(store, store.user_name)
    try:
        store.get_document(store.user_name)
    except FirestoreNotFound:
        return deleted
    store.commit([{"delete": store.user_name}])
    return deleted + 1
