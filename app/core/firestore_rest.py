"""Owner-scoped Firestore REST gateway.

All data operations are constrained to users/{verified_uid}. A TenantStore
owns one reusable AuthorizedSession, so token refresh and HTTP connections
can be reused during a single request/workflow.
"""

import json
import math
import re

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from app.core.config import settings

FIRESTORE_SCOPE = "https://www.googleapis.com/auth/datastore"
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
POST_ID = re.compile(r"^[0-9]{1,128}$")
MAX_QUERY_POSTS = 10000


class FirestoreUnavailable(RuntimeError):
    pass


class FirestoreNotFound(RuntimeError):
    pass


class FirestoreConflict(RuntimeError):
    pass


def _credentials():
    raw = settings.firebase_service_account_json
    if raw:
        return service_account.Credentials.from_service_account_info(
            json.loads(raw), scopes=[FIRESTORE_SCOPE]
        )
    credentials, _ = google.auth.default(scopes=[FIRESTORE_SCOPE])
    return credentials


PRODUCTION_ROOT = "https://firestore.googleapis.com/v1/"
_EMULATOR_HOST = re.compile(r"(localhost|127\.0\.0\.1|\[::1\]|[A-Za-z0-9-]{1,63}):[0-9]{2,5}\Z")


def emulator_root(config=None) -> str | None:
    """Base URL of a local Firestore emulator, or None for real Firestore.

    The emulator accepts any data without credentials, so production never
    uses it, whatever the environment says.
    """
    config = settings if config is None else config
    host = getattr(config, "firestore_emulator_host", "")
    if not isinstance(host, str) or not host.strip():
        return None
    if getattr(config, "production", True):
        raise FirestoreUnavailable("The Firestore emulator is not allowed in production")
    host = host.strip()
    if not _EMULATOR_HOST.fullmatch(host):
        raise FirestoreUnavailable("Invalid FIRESTORE_EMULATOR_HOST")
    return f"http://{host}/v1/"


def _emulator_session():
    import requests

    session = requests.Session()
    # The emulator treats this token as an administrator, like the server SDK.
    session.headers["Authorization"] = "Bearer owner"
    return session


def _session():
    if emulator_root() is not None:
        return _emulator_session()
    return AuthorizedSession(_credentials())


def encode_value(value):
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Firestore doubles must be finite")
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    raise ValueError("Unsupported Firestore field type")


def encode_fields(data: dict) -> dict:
    return {key: encode_value(value) for key, value in data.items()}


def decode_value(value: dict):
    if "stringValue" in value:
        return value["stringValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        raw = value["doubleValue"]
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise FirestoreUnavailable("Invalid Firestore double")
        try:
            result = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise FirestoreUnavailable("Invalid Firestore double") from exc
        if not math.isfinite(result):
            raise FirestoreUnavailable("Non-finite Firestore double")
        return result
    if "booleanValue" in value:
        return value["booleanValue"]
    if "nullValue" in value:
        return None
    raise FirestoreUnavailable("Unsupported stored field type")


def decode_fields(fields: dict) -> dict:
    return {key: decode_value(value) for key, value in fields.items()}


class TenantStore:
    """Firestore REST access for one verified Firebase user."""

    def __init__(self, verified_uid: str):
        if not isinstance(verified_uid, str) or not SAFE_ID.fullmatch(verified_uid):
            raise ValueError("Invalid verified UID")
        if not settings.firebase_ready:
            raise FirestoreUnavailable("Firestore is not configured")

        self.uid = verified_uid
        self.database = f"projects/{settings.firebase_project_id}/databases/(default)"
        self.base_name = f"{self.database}/documents"
        self.user_name = f"{self.base_name}/users/{self.uid}"
        self.base = f"https://firestore.googleapis.com/v1/{self.base_name}"
        self.user_document_url = f"https://firestore.googleapis.com/v1/{self.user_name}"
        self.users_collection_url = f"{self.base}/users"
        self._http_session = None

    def _check_resource(self, resource: str) -> None:
        if resource == self.user_name:
            return
        if not isinstance(resource, str) or not resource.startswith(self.user_name + "/"):
            raise ValueError("Resource outside tenant")
        relative = resource[len(self.user_name) + 1:]
        if not relative or not all(SAFE_ID.fullmatch(s) for s in relative.split("/")):
            raise ValueError("Invalid tenant resource")

    def _request(self, method: str, url: str, **kwargs):
        try:
            root = emulator_root()
            if root is not None and url.startswith(PRODUCTION_ROOT):
                url = root + url[len(PRODUCTION_ROOT):]
            if self._http_session is None:
                self._http_session = _session()
            response = self._http_session.request(method, url, timeout=15, **kwargs)
        except Exception as exc:
            raise FirestoreUnavailable("Firestore connection failed") from exc

        if response.status_code in (200, 201):
            try:
                return response.json()
            except ValueError:
                # Firestore streaming RPCs may be returned as newline-delimited JSON.
                try:
                    lines = [json.loads(s) for s in response.text.splitlines() if s.strip()]
                    if lines and all(isinstance(item, dict) for item in lines):
                        return lines
                except (AttributeError, TypeError, ValueError):
                    pass
                raise FirestoreUnavailable("Invalid Firestore response")

        if response.status_code == 404:
            raise FirestoreNotFound("Document not found")
        if response.status_code in (409, 412):
            raise FirestoreConflict("Write conflict")
        if response.status_code == 400:
            try:
                status = response.json().get("error", {}).get("status")
            except (ValueError, TypeError, AttributeError):
                status = None
            if status in ("ABORTED", "FAILED_PRECONDITION", "ALREADY_EXISTS"):
                raise FirestoreConflict("Write conflict")
        raise FirestoreUnavailable("Firestore request was rejected")

    def get_document(self, resource: str) -> dict:
        self._check_resource(resource)
        return self._request("GET", f"https://firestore.googleapis.com/v1/{resource}")

    def list_collection(self, resource: str, page_size: int = 20,
                        page_token: str = "", order_by: str = "") -> dict:
        self._check_resource(resource)
        if not 1 <= page_size <= 50:
            raise ValueError("Invalid page size")
        if not isinstance(page_token, str) or len(page_token) > 2048:
            raise ValueError("Invalid page token")
        params = {"pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        if order_by:
            params["orderBy"] = order_by
        return self._request("GET", f"https://firestore.googleapis.com/v1/{resource}", params=params)

    def run_query(self, parent: str, structured_query: dict) -> list:
        """Run a direct-child collection query under a verified tenant parent."""
        self._check_resource(parent)
        if not isinstance(structured_query, dict):
            raise ValueError("Invalid structured query")
        sources = structured_query.get("from")
        if (not isinstance(sources, list) or len(sources) != 1
                or not isinstance(sources[0], dict)
                or not SAFE_ID.fullmatch(str(sources[0].get("collectionId", "")))
                or sources[0].get("allDescendants", False)):
            raise ValueError("Only direct-child collection queries are permitted")
        response = self._request(
            "POST", f"https://firestore.googleapis.com/v1/{parent}:runQuery",
            json={"structuredQuery": structured_query},
        )
        if not isinstance(response, list):
            raise FirestoreUnavailable("Invalid Firestore query stream")
        for item in response:
            if not isinstance(item, dict):
                raise FirestoreUnavailable("Invalid Firestore query item")
            document = item.get("document")
            if document is not None:
                name = document.get("name") if isinstance(document, dict) else None
                if not isinstance(name, str):
                    raise FirestoreUnavailable("Invalid Firestore document")
                self._check_resource(name)
                if not name.startswith(parent + "/" + sources[0]["collectionId"] + "/"):
                    raise FirestoreUnavailable("Query returned an unexpected document")
                if len(name.split("/")) != len(parent.split("/")) + 2:
                    raise FirestoreUnavailable("Query returned a nested document")
        return response

    def query_posts_in_period(self, client_id: str, start: str,
                              end: str, page_size: int = 50) -> list:
        """Read one calendar window via stable (published_date, __name__) cursors."""
        parent = self.client_name(client_id)
        if (not isinstance(start, str) or not isinstance(end, str)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end)
                or start > end or not 1 <= page_size <= 100):
            raise ValueError("Invalid reporting date range")
        field = {"fieldPath": "published_date"}
        query = {
            "from": [{"collectionId": "posts"}],
            "where": {"compositeFilter": {"op": "AND", "filters": [
                {"fieldFilter": {"field": field, "op": "GREATER_THAN_OR_EQUAL",
                                 "value": {"stringValue": start}}},
                {"fieldFilter": {"field": field, "op": "LESS_THAN_OR_EQUAL",
                                 "value": {"stringValue": end}}},
            ]}},
            "orderBy": [
                {"field": field, "direction": "ASCENDING"},
                {"field": {"fieldPath": "__name__"}, "direction": "ASCENDING"},
            ],
            "limit": page_size,
        }
        results = []
        previous = None
        while True:
            stream = self.run_query(parent, query)
            documents = [item["document"] for item in stream if "document" in item]
            if len(documents) > page_size:
                raise FirestoreUnavailable("Firestore page exceeded requested limit")
            for document in documents:
                name = document["name"]
                fields = decode_fields(document.get("fields", {}))
                published = fields.get("published_date")
                if (not POST_ID.fullmatch(name.rsplit("/", 1)[-1])
                        or not isinstance(published, str)
                        or not start <= published <= end):
                    raise FirestoreUnavailable("Invalid monthly post data")
                key = (published, name)
                if previous is not None and key <= previous:
                    raise FirestoreUnavailable("Firestore pagination did not advance")
                previous = key
                results.append(document)
                if len(results) > MAX_QUERY_POSTS:
                    raise FirestoreUnavailable("Monthly query exceeds the current safety limit")
            if len(documents) < page_size:
                return results
            if previous is None:
                raise FirestoreUnavailable("Firestore returned no pagination cursor")
            query["startAt"] = {
                "values": [
                    {"stringValue": previous[0]},
                    {"referenceValue": previous[1]},
                ],
                "before": False,
            }

    def batch_get_documents(self, resources: list) -> dict:
        """Return {resource: found_document_or_None}; reject incomplete/mixed streams."""
        if not isinstance(resources, list) or len(resources) > 100:
            raise ValueError("Batch must contain at most 100 document names")
        if len(resources) != len(set(resources)):
            raise ValueError("Duplicate batch document")
        for resource in resources:
            self._check_resource(resource)
        if not resources:
            return {}
        responses = self._request(
            "POST", f"https://firestore.googleapis.com/v1/{self.database}/documents:batchGet",
            json={"documents": resources},
        )
        if not isinstance(responses, list):
            raise FirestoreUnavailable("Invalid Firestore batch response")
        result = {}
        expected = set(resources)
        for response in responses:
            if not isinstance(response, dict):
                raise FirestoreUnavailable("Invalid Firestore batch item")
            if "found" in response:
                document = response["found"]
                name = document.get("name") if isinstance(document, dict) else None
            elif "missing" in response:
                document = None
                name = response["missing"]
            else:
                # A readTime-only stream entry is not a document response.
                continue
            if not isinstance(name, str) or name not in expected or name in result:
                raise FirestoreUnavailable("Batch returned a missing, duplicate or foreign name")
            self._check_resource(name)
            result[name] = document
        if set(result) != expected:
            raise FirestoreUnavailable("Incomplete Firestore batch response")
        return result

    def commit(self, writes: list) -> dict:
        if not writes or len(writes) > 20:
            raise ValueError("Invalid commit size")
        for write in writes:
            if "update" in write:
                resource = write["update"]["name"]
            elif "delete" in write:
                resource = write["delete"]
            else:
                raise ValueError("Unsupported write operation")
            self._check_resource(resource)
        return self._request("POST", f"{self.base}:commit", json={"writes": writes})

    def get_user_with_version(self):
        document = self.get_document(self.user_name)
        return decode_fields(document.get("fields", {})), document["updateTime"]

    def ensure_profile(self, verified_email: str):
        payload = {"fields": encode_fields({
            "email": verified_email, "created_via": "firebase_auth",
        })}
        try:
            self._request("POST", self.users_collection_url,
                          params={"documentId": self.uid}, json=payload)
        except FirestoreConflict:
            return

    def get_profile(self):
        fields, _ = self.get_user_with_version()
        return {"email": fields.get("email", "")}

    def client_name(self, client_id: str) -> str:
        if not isinstance(client_id, str) or not SAFE_ID.fullmatch(client_id):
            raise ValueError("Invalid client ID")
        return f"{self.user_name}/clients/{client_id}"

    def client_document_url(self, client_id: str) -> str:
        return "https://firestore.googleapis.com/v1/" + self.client_name(client_id)

    def get_client(self, client_id: str) -> dict:
        document = self.get_document(self.client_name(client_id))
        data = decode_fields(document.get("fields", {}))
        data["id"] = client_id
        data["_update_time"] = document["updateTime"]
        return data

    def list_clients(self, page_token: str = ""):
        response = self.list_collection(f"{self.user_name}/clients", page_size=20,
                                        page_token=page_token, order_by="created_at desc")
        clients = []
        for document in response.get("documents", []):
            data = decode_fields(document.get("fields", {}))
            data["id"] = document["name"].rsplit("/", 1)[-1]
            clients.append(data)
        return clients, response.get("nextPageToken", "")

    def list_reports(self, client_id: str):
        resource = self.client_name(client_id) + "/reports"
        response = self.list_collection(resource, page_size=20, order_by="period desc")
        reports = []
        for document in response.get("documents", []):
            data = decode_fields(document.get("fields", {}))
            data["id"] = document["name"].rsplit("/", 1)[-1]
            reports.append(data)
        return reports, response.get("nextPageToken", "")
