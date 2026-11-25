"""Best-effort aggregate request counters for private report links.

These fields count successful HTML opens or generated PDF responses, not
unique people or verified reading. No IP, user-agent or browsing log is kept.
A counter write is never required for an already-authorized response.
"""

from datetime import (
    datetime,
    timezone,
)

from app.core.firestore_rest import (
    FirestoreConflict,
    FirestoreNotFound,
    FirestoreUnavailable,
    decode_fields,
)

from app.services.client_workspace import (
    make_update,
    parse_timestamp,
)

from app.services.share_case import _digest


COUNTERS = {
    "html": (
        "html_open_count",
        "last_html_opened_at",
    ),
    "pdf": (
        "pdf_open_count",
        "last_pdf_opened_at",
    ),
}

MAX_COUNTER = 2**63 - 1


class ShareActivityService:

    def __init__(self, store):
        self.store = store

    def _eligible(
        self,
        link,
        client_id,
        version,
        counter_name,
    ):
        if (
            not isinstance(link, dict)
            or not isinstance(version, str)
            or not version
        ):
            return False

        if link.get("revoked"):
            return False

        if (
            link.get("owner_uid")
            != getattr(self.store, "uid", None)
            or link.get("client_id") != client_id
        ):
            return False

        # Verification supplies effective_expires_at for a trial link
        # extended after provider-verified payment. Legacy, already-
        # verified links may expose only expires_at or neither field.
        # Do not reject them solely because metadata is absent.
        expires = (
            link.get("effective_expires_at")
            or link.get("expires_at")
        )

        if expires:
            deadline = parse_timestamp(
                expires
            )

            if (
                deadline is None
                or deadline <= datetime.now(
                    timezone.utc
                )
            ):
                return False

        count = link.get(
            counter_name,
            0,
        )

        return (
            type(count) is int
            and 0 <= count < MAX_COUNTER
        )

    def record(
        self,
        client_id,
        token,
        link,
        version,
        kind,
    ):
        """Count one verified response, retrying at most one version conflict."""

        if kind not in COUNTERS:
            raise ValueError(
                "Unsupported access type"
            )

        counter_name, time_name = (
            COUNTERS[kind]
        )

        if not self._eligible(
            link,
            client_id,
            version,
            counter_name,
        ):
            return False

        try:
            name = (
                self.store.client_name(
                    client_id
                )
                + "/shares/"
                + _digest(token)
            )

        except (
            FirestoreNotFound,
            ValueError,
        ):
            return False

        for attempt in range(2):
            values = {
                counter_name: (
                    link.get(counter_name, 0)
                    + 1
                ),
                time_name: datetime.now(
                    timezone.utc
                ).isoformat(),
            }

            try:
                self.store.commit([
                    make_update(
                        name,
                        values,
                        version,
                    )
                ])

                return True

            except FirestoreUnavailable:
                return False

            except FirestoreConflict:
                if attempt:
                    return False

                # Read the current document once.
                # Do not retry a revoked, expired
                # or different owner's link.

                if not hasattr(
                    self.store,
                    "get_document",
                ):
                    return False

                try:
                    current = (
                        self.store.get_document(
                            name
                        )
                    )

                    link = decode_fields(
                        current.get(
                            "fields", {}
                        )
                    )

                    version = current.get(
                        "updateTime", ""
                    )

                except (
                    FirestoreNotFound,
                    FirestoreUnavailable,
                    KeyError,
                    ValueError,
                ):
                    return False

                if not self._eligible(
                    link,
                    client_id,
                    version,
                    counter_name,
                ):
                    return False

        return False
