"""Strict, memory-only parser for X Analytics Content CSV.

The reference format is the file X offers as the account's Content export:
UTF-8, comma-separated, 17 columns, dates such as "Sun, Nov 9, 2026".

Files that a spreadsheet re-saved are accepted when nothing is lost:
column names in another letter case, semicolon or tab separators, UTF-16
with a byte-order mark, grouped thousands ("1,234") and Post ids that the
spreadsheet turned into rounded numbers ("2.12174E+18") while the Post Link
still carries the exact id. Anything that cannot be recovered without
guessing is refused with the row number.

A post that appears more than once is never dropped silently: identical
rows are merged and reported, and rows that disagree must be resolved by
the user (``duplicate_choices``) before they can be imported.
"""

import csv
import hashlib
import html
import io
import json
import re

from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit


MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 10_000
MAX_TEXT = 12_000
MAX_CONFLICTS = 50


COLUMNS = {
    "Post id": "post_id",
    "Date": "published_date",
    "Post text": "post_text",
    "Post Link": "post_url",
    "Impressions": "impressions",
    "Likes": "likes",
    "Engagements": "engagements",
    "Bookmarks": "bookmarks",
    "Shares": "shares",
    "New follows": "new_follows",
    "Replies": "replies",
    "Reposts": "reposts",
    "Profile visits": "profile_visits",
    "Detail Expands": "detail_expands",
    "URL Clicks": "url_clicks",
    "Hashtag Clicks": "hashtag_clicks",
    "Permalink Clicks": "permalink_clicks",
}


ALIASES = {
    "Post ID": "Post id",
    "Post Id": "Post id",
    "Post URL": "Post Link",
}


REQUIRED = {
    "Post id",
    "Date",
    "Post text",
}


METRICS = tuple(
    field
    for field in COLUMNS.values()
    if field not in {
        "post_id",
        "published_date",
        "post_text",
        "post_url",
    }
)


ENGAGEMENT_PARTS = (
    "likes",
    "bookmarks",
    "shares",
    "new_follows",
    "replies",
    "reposts",
    "profile_visits",
    "detail_expands",
    "url_clicks",
    "hashtag_clicks",
    "permalink_clicks",
)


MONTHS = {
    name: index
    for index, name in enumerate(
        (
            "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        ),
        start=1,
    )
}


WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


DATE_RE = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun),"
    r"\s+([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})$"
)

ISO_DATETIME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:?\d{2})?$"
)


FILENAME_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})"
)


ID_RE = re.compile(r"^[0-9]{1,128}$")

STATUS_PATH_RE = re.compile(
    r"^/(?:[A-Za-z0-9_]{1,15}|i/web)/status/([0-9]{1,128})/?$"
)

SPREADSHEET_NUMBER_RE = re.compile(r"^\d+(?:[.,]\d+)?[eE]\+?\d{1,3}$")

GROUPED_RE = re.compile(r"^\d{1,3}([,. \u00a0\u202f])\d{3}(?:\1\d{3})*$")

DELIMITERS = (",", ";", "\t")

ROW_FIELDS = ("post_id", "published_date", "post_text", "post_url") + METRICS


class CsvValidationError(ValueError):
    pass


def _header_key(value: str) -> str:
    return " ".join(value.replace("\ufeff", "").split()).casefold()


HEADER_LOOKUP = {
    **{_header_key(name): name for name in COLUMNS},
    **{_header_key(alias): name for alias, name in ALIASES.items()},
}


def _canonical_headers(headers) -> list:
    return [
        HEADER_LOOKUP.get(_header_key(header), header.strip())
        for header in headers
    ]


def _date(raw: str, row_number: int) -> str:
    raw = raw.strip()

    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw).isoformat()

        timestamp = ISO_DATETIME_RE.fullmatch(raw)

        if timestamp:
            return date.fromisoformat(timestamp[1]).isoformat()

        match = DATE_RE.fullmatch(raw)

        if match is None or match[2] not in MONTHS:
            raise ValueError()

        weekday, month, day, year = match.groups()
        value = date(int(year), MONTHS[month], int(day))

        if WEEKDAYS[value.weekday()] != weekday:
            raise ValueError()

        return value.isoformat()

    except ValueError as exc:
        raise CsvValidationError(
            f"Row {row_number}: unsupported publication date "
            f"\"{raw[:30]}\". Use the date format of the X export "
            "(for example \"Mon, Nov 9, 2026\") or YYYY-MM-DD."
        ) from exc


def _metric(raw: str, row_number: int, field: str):
    value = raw.strip()

    if not value:
        return None

    if not re.fullmatch(r"[0-9]+", value):
        grouped = GROUPED_RE.fullmatch(value)

        if grouped is None:
            raise CsvValidationError(
                f"Row {row_number}: invalid {field}."
            )

        value = value.replace(grouped[1], "")

    parsed = int(value)

    if parsed > 2**63 - 1:
        raise CsvValidationError(
            f"Row {row_number}: {field} exceeds integer range."
        )

    return parsed


def _url(raw: str, row_number: int) -> str:
    value = raw.strip()

    if not value:
        return ""

    if len(value) > 2048 or any(ord(char) < 32 for char in value):
        raise CsvValidationError(f"Row {row_number}: invalid post URL.")

    try:
        parts = urlsplit(value)
        port = parts.port

    except ValueError as exc:
        raise CsvValidationError(
            f"Row {row_number}: invalid post URL."
        ) from exc

    if (
        parts.scheme != "https"
        or parts.hostname not in {
            "x.com",
            "www.x.com",
            "twitter.com",
            "www.twitter.com",
        }
        or parts.username
        or parts.password
        or port is not None
    ):
        raise CsvValidationError(f"Row {row_number}: unsupported post URL.")

    return value


def _status_id(url: str):
    if not url:
        return None

    match = STATUS_PATH_RE.fullmatch(urlsplit(url).path)
    return match[1] if match else None


def _spreadsheet_rounded(post_id: str) -> set:
    """How a spreadsheet writes a long id after keeping 15 significant digits."""
    exact = Decimal(post_id)
    rounded = {int(Decimal(format(exact, ".15g")))}

    try:
        rounded.add(int(Decimal(format(float(post_id), ".15g"))))
    except (OverflowError, ValueError):
        pass

    return {str(value) for value in rounded}


def _close_to(display: str, post_id: str) -> bool:
    """A scientific-notation display that rounds to this id."""
    try:
        value = Decimal(display.replace(",", "."))
    except InvalidOperation:
        return False

    exact = Decimal(post_id)
    return exact > 0 and abs(value - exact) / exact < Decimal("1e-4")


def _post_id(raw: str, link_id, row_number: int):
    """Return (post_id, repaired)."""
    value = raw.strip()

    if ID_RE.fullmatch(value):
        if link_id is None or value == link_id:
            return value, False

        if value in _spreadsheet_rounded(link_id):
            return link_id, True

        raise CsvValidationError(
            f"Row {row_number}: Post id does not match the Post Link."
        )

    if (
        link_id is not None
        and SPREADSHEET_NUMBER_RE.fullmatch(value)
        and _close_to(value, link_id)
    ):
        return link_id, True

    raise CsvValidationError(
        f"Row {row_number}: invalid Post id. If the file was opened in a "
        "spreadsheet, upload the original file downloaded from X."
    )


def _open_text(raw: bytes):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        if b"\x00" in raw:
            raise CsvValidationError("CSV contains a forbidden null byte.")

        encoding = "utf-8-sig"

    # Decode incrementally rather than holding the whole decoded CSV and a
    # second StringIO copy in memory. The parsed rows are still retained
    # for the preview-and-confirm workflow.
    return io.TextIOWrapper(
        io.BytesIO(raw),
        encoding=encoding,
        errors="strict",
        newline="",
    )


def _choose_delimiter(header_line: str):
    """The separator whose header row names every required column."""
    ranked = sorted(
        DELIMITERS,
        key=lambda candidate: -header_line.count(candidate),
    )

    first = None

    for candidate in ranked:
        headers = next(csv.reader([header_line], delimiter=candidate), [])
        canonical = _canonical_headers(headers)

        if first is None:
            first = canonical

        if REQUIRED.issubset(canonical):
            return candidate, canonical

    missing = sorted(REQUIRED - set(first or []))
    raise CsvValidationError(
        "Missing required columns: "
        + ", ".join(missing)
        + ". Upload the Content CSV downloaded from X; a spreadsheet "
        "should save it as CSV UTF-8 (comma-delimited)."
    )


def _identity(raw: bytes, choices) -> str:
    digest = hashlib.sha256(raw)

    if choices:
        digest.update(b"\n#duplicate-choices:")
        digest.update(
            json.dumps(choices, sort_keys=True, separators=(",", ":")).encode()
        )

    return digest.hexdigest()


def _group_duplicates(numbered_rows):
    """Split rows into unique posts, merged duplicates and conflicting ones."""
    groups = {}

    for number, row in numbered_rows:
        groups.setdefault(row["post_id"], []).append((number, row))

    unique = []
    conflicts = {}
    merged = 0

    for post_id, members in groups.items():
        first = members[0][1]

        if all(
            all(row[field] == first[field] for field in ROW_FIELDS)
            for _, row in members[1:]
        ):
            unique.append(first)
            merged += len(members) - 1
        else:
            conflicts[post_id] = members

    return unique, conflicts, merged


def _conflict_summary(conflicts) -> list:
    return [
        {
            "post_id": post_id,
            "rows": [
                {
                    "row": number,
                    "date": row["published_date"],
                    "text": row["post_text"][:120],
                    "impressions": row["impressions"],
                    "engagements": row["engagements"],
                    "likes": row["likes"],
                }
                for number, row in members
            ],
        }
        for post_id, members in conflicts.items()
    ]


def _resolve(conflicts, choices):
    if not isinstance(choices, dict) or set(choices) != set(conflicts):
        raise CsvValidationError(
            "Choose which row to keep for each post that appears more "
            "than once."
        )

    chosen = []

    for post_id, members in conflicts.items():
        selected = choices[post_id]
        row = next(
            (row for number, row in members if number == selected),
            None,
        )

        if type(selected) is not int or row is None:
            raise CsvValidationError(
                f"Choose one of the listed rows for post {post_id}."
            )

        chosen.append(row)

    return chosen


def parse_duplicate_choices(raw: str):
    """Read the X-Duplicate-Choices header: {"post id": row number, ...}."""
    if not raw:
        return None

    if len(raw) > 8192:
        raise CsvValidationError("Too many duplicate choices.")

    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise CsvValidationError("Invalid duplicate choices.") from exc

    if (
        not isinstance(value, dict)
        or len(value) > MAX_CONFLICTS
        or not all(
            isinstance(key, str) and ID_RE.fullmatch(key)
            and type(number) is int and 2 <= number <= MAX_ROWS + 1
            for key, number in value.items()
        )
    ):
        raise CsvValidationError("Invalid duplicate choices.")

    return value


def parse_content_csv(
    raw: bytes,
    filename: str = "",
    duplicate_choices=None,
) -> dict:
    if not raw or len(raw) > MAX_BYTES:
        raise CsvValidationError(
            "CSV must be between 1 byte and 10 MiB."
        )

    stream = _open_text(raw)

    numbered = []
    partial_engagements = 0
    mismatches = 0
    repaired = 0
    empty_texts = 0

    try:
        header_line = stream.readline()

        if not header_line.strip():
            raise CsvValidationError("CSV header is missing.")

        delimiter, canonical = _choose_delimiter(header_line)

        if len(canonical) != len(set(canonical)):
            raise CsvValidationError("CSV contains duplicate columns.")

        # The header line is already read; the reader starts at row 2.
        reader = csv.DictReader(
            stream,
            fieldnames=canonical,
            delimiter=delimiter,
            strict=True,
        )

        unknown = sorted(set(canonical) - set(COLUMNS))
        absent = sorted(set(COLUMNS) - set(canonical))

        warnings = []

        if unknown:
            warnings.append(
                "Unknown columns ignored: " + ", ".join(unknown)
            )

        if absent:
            warnings.append(
                "Absent columns stored as missing, not zero: "
                + ", ".join(absent)
            )

        metric_columns = [
            (csv_column, field)
            for csv_column, field in COLUMNS.items()
            if field in METRICS
        ]

        for number, record in enumerate(reader, start=2):
            if len(numbered) >= MAX_ROWS:
                raise CsvValidationError(
                    "CSV exceeds 10,000 post rows."
                )

            if (
                None in record
                or any(value is None for value in record.values())
            ):
                raise CsvValidationError(
                    f"Row {number}: inconsistent field count."
                )

            post_url = _url(record.get("Post Link", ""), number)
            post_id, fixed = _post_id(
                record["Post id"],
                _status_id(post_url),
                number,
            )
            repaired += fixed

            original_date = record["Date"].strip()
            published = _date(original_date, number)

            text = html.unescape(record["Post text"])

            if len(text) > MAX_TEXT:
                raise CsvValidationError(
                    f"Row {number}: post text too long."
                )

            if not text.strip():
                empty_texts += 1

            row = {
                "post_id": post_id,
                "original_date": original_date,
                "published_date": published,
                "post_text": text,
                "post_url": post_url,
            }

            for csv_column, field in metric_columns:
                row[field] = _metric(
                    record.get(csv_column, ""),
                    number,
                    csv_column,
                )

            complete_components = all(
                row[field] is not None for field in ENGAGEMENT_PARTS
            )

            if row["engagements"] is not None:
                if complete_components:
                    total = sum(row[field] for field in ENGAGEMENT_PARTS)

                    if total != row["engagements"]:
                        mismatches += 1

                else:
                    partial_engagements += 1

            numbered.append((number, row))

    except UnicodeError as exc:
        raise CsvValidationError(
            "CSV must use UTF-8 encoding. Upload the original file "
            "downloaded from X, or save it as CSV UTF-8 in your "
            "spreadsheet app."
        ) from exc

    except csv.Error as exc:
        raise CsvValidationError(
            "Malformed CSV structure."
        ) from exc

    finally:
        stream.close()

    if not numbered:
        raise CsvValidationError("CSV has no post records.")

    unique, conflicts, merged = _group_duplicates(numbered)

    if len(conflicts) > MAX_CONFLICTS:
        raise CsvValidationError(
            f"{len(conflicts)} posts appear more than once with different "
            "values. Export the period again from X; Reportlume can "
            f"resolve at most {MAX_CONFLICTS} such posts in one file."
        )

    choices = duplicate_choices if conflicts else None
    rows = unique + (_resolve(conflicts, choices) if choices is not None else [])
    unresolved = bool(conflicts) and choices is None

    # Statistics describe each post once; an unresolved post counts by its
    # first row until the user chooses.
    described = rows + (
        [members[0][1] for members in conflicts.values()]
        if unresolved else []
    )

    by_month = Counter(row["published_date"][:7] for row in described)
    dates = [row["published_date"] for row in described]

    if merged:
        warnings.append(
            f"{merged} exact duplicate rows were merged; each post is "
            "imported once."
        )

    if conflicts:
        warnings.append(
            f"{len(conflicts)} posts appear more than once with different "
            "values. Choose which row to keep for each before importing."
        )

    if repaired:
        warnings.append(
            f"{repaired} Post ids changed by a spreadsheet were restored "
            "from the Post Link."
        )

    if empty_texts:
        warnings.append(f"{empty_texts} rows have no post text.")

    if mismatches:
        warnings.append(
            f"{mismatches} rows have an Engagements "
            "component mismatch; values were preserved."
        )

    if partial_engagements:
        warnings.append(
            f"{partial_engagements} rows have an Engagements "
            "value but incomplete component coverage; "
            "no component-sum check was possible."
        )

    if len(by_month) > 1:
        warnings.append(
            "Posts span multiple calendar months; "
            "choose each reporting period separately."
        )

    hint = None
    match = FILENAME_RE.search(filename)

    if match:
        try:
            hint = {
                "start": date.fromisoformat(match[1]).isoformat(),
                "end": date.fromisoformat(match[2]).isoformat(),
            }

        except ValueError:
            pass

    file_sha256 = hashlib.sha256(raw).hexdigest()

    return {
        "sha256": _identity(raw, choices),
        "file_sha256": file_sha256,
        "rows": rows,
        "row_count": len(described),
        "columns": canonical,
        "missing_columns": absent,
        "warnings": warnings,
        "published_start": min(dates),
        "published_end": max(dates),
        "months": dict(sorted(by_month.items())),
        "possible_replies": sum(
            row["post_text"].lstrip().startswith("@") for row in described
        ),
        "zero_impressions": sum(
            row["impressions"] == 0 for row in described
        ),
        "engagement_mismatches": mismatches,
        "duplicates_merged": merged,
        "duplicate_conflicts": _conflict_summary(conflicts),
        "needs_duplicate_choice": unresolved,
        "repaired_post_ids": repaired,
        "suggested_export": hint,
    }
