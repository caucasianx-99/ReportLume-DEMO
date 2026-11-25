"""Consistent default reporting periods for monthly client workflows.

The default is the previous *completed* calendar month in UTC. Users can
explicitly select any valid month; this is a navigation default, not a
claim that the data represents a fully completed export.
"""

from datetime import date, datetime, timedelta, timezone


def previous_completed_month(reference=None) -> str:
    """Return YYYY-MM for the month preceding the reference date.

    A timezone-aware datetime is converted to UTC before selection. Supplying
    a date is useful for deterministic calendar boundary tests.
    """

    if reference is None:
        today = datetime.now(timezone.utc).date()
    elif isinstance(reference, datetime):
        if reference.tzinfo is None:
            raise ValueError("Reference datetime must include a timezone")
        today = reference.astimezone(timezone.utc).date()
    elif isinstance(reference, date):
        today = reference
    else:
        raise TypeError("Reference must be a date or aware datetime")

    first_of_month = today.replace(day=1)
    previous_day = first_of_month - timedelta(days=1)
    return previous_day.strftime("%Y-%m")


def is_unfinished_month(period: str, reference=None) -> bool:
    # Current or future calendar month, measured in UTC.
    if not isinstance(period, str) or len(period) != 7 or period[4] != "-":
        raise ValueError("Invalid reporting month")

    try:
        year, month = period.split("-", 1)
        if (
            len(year) != 4
            or len(month) != 2
            or not year.isascii()
            or not month.isascii()
            or not year.isdigit()
            or not month.isdigit()
        ):
            raise ValueError("Invalid reporting month")

        selected = date(int(year), int(month), 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid reporting month") from exc

    if reference is None:
        today = datetime.now(timezone.utc).date()
    elif isinstance(reference, datetime):
        if reference.tzinfo is None:
            raise ValueError("Reference datetime must include a timezone")
        today = reference.astimezone(timezone.utc).date()
    elif isinstance(reference, date):
        today = reference
    else:
        raise TypeError("Reference must be a date or aware datetime")

    return selected >= today.replace(day=1)
