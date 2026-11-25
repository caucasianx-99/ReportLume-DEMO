"""Identifiers and conservative dates for immutable X CSV observations.

New writes are always scoped to both their measurement key and the exact
uploaded file. Legacy key-only IDs remain readable, but are never created.
"""

import hashlib
import re

from datetime import date


UPLOAD_ID_RE = re.compile(r"^[a-f0-9]{64}$")

PRECISIONS = frozenset({
    "confirmed_minute",
    "date_only_time_unknown",
})


def legacy_observation_id(measurement_key: str) -> str:
    """Read-only compatibility with existing observation document names."""

    if not isinstance(measurement_key, str) or not measurement_key:
        raise ValueError("Invalid measurement key")

    return hashlib.sha256(
        measurement_key.encode("utf-8")
    ).hexdigest()


def observation_id(measurement_key: str, upload_id: str) -> str:
    """Use the same identity on retries of the same CSV, not across CSVs."""

    if not isinstance(measurement_key, str) or not measurement_key:
        raise ValueError("Invalid measurement key")

    if (
        not isinstance(upload_id, str)
        or not UPLOAD_ID_RE.fullmatch(upload_id)
    ):
        raise ValueError("Invalid upload ID")

    return hashlib.sha256(
        (measurement_key + upload_id).encode("utf-8")
    ).hexdigest()


def conservative_age_days(
    published_date: str,
    measurement_date: str,
):
    """Date-only X exports cannot establish exact hours of post exposure."""

    try:
        published = date.fromisoformat(published_date)
        measured = date.fromisoformat(measurement_date)

    except (TypeError, ValueError):
        return None

    days = (measured - published).days

    if days < 0:
        return None

    return max(0, days - 1)


def valid_measurement(observation: dict) -> bool:
    """Accept a dated observation without inventing its clock time."""

    if not isinstance(observation, dict):
        return False

    precision = observation.get("measurement_precision")

    if precision not in PRECISIONS:
        return False

    if precision == "confirmed_minute":
        return bool(observation.get("measurement_at"))

    return observation.get("measurement_at") in (None, "")
