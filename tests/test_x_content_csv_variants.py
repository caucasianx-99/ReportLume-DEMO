"""Real X export structure and the ways customers change the file.

``fixtures/x_content_export.csv`` copies the exact structure of a real X
Content export (header, quoting, date format) with fictional posts, ids and
numbers. ``EXCEL`` is that file as Excel re-saved the real export: Post ids
turned into rounded numbers such as 2.04123E+18 and lines end with CRLF,
while the Post Link still carries the exact id.
"""

import csv
import io
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.x_content_csv import (
    MAX_CONFLICTS,
    CsvValidationError,
    parse_content_csv,
    parse_duplicate_choices,
)

FIXTURES = Path(__file__).parent / "fixtures"
ORIGINAL = (FIXTURES / "x_content_export.csv").read_bytes()


def excel_number(post_id: str) -> str:
    """Excel's General format: six significant digits, e.g. 2.04123E+18."""
    mantissa, exponent = f"{Decimal(post_id):.5E}".split("E")
    return f"{mantissa.rstrip('0').rstrip('.')}E{exponent}"


def _excel_copy() -> bytes:
    rows = list(csv.reader(io.StringIO(ORIGINAL.decode("utf-8"), newline="")))
    for row in rows[1:]:
        row[0] = excel_number(row[0])
    out = io.StringIO(newline="")
    csv.writer(out, lineterminator="\r\n").writerows(rows)
    return out.getvalue().encode("utf-8")


EXCEL = _excel_copy()


def table(raw=ORIGINAL):
    return list(csv.reader(io.StringIO(raw.decode("utf-8"), newline="")))


def build(rows, *, delimiter=",", terminator="\n", encoding="utf-8"):
    out = io.StringIO(newline="")
    csv.writer(out, delimiter=delimiter, lineterminator=terminator).writerows(rows)
    return out.getvalue().encode(encoding)


def with_rows(extra, base=None):
    rows = table() if base is None else base
    return build(rows + extra)


# ------------------------------------------------------- real export shape

def test_real_export_structure_is_read_without_warnings():
    parsed = parse_content_csv(ORIGINAL, "account_analytics_content_2026-10-01_2026-10-31.csv")

    assert parsed["row_count"] == 10
    assert parsed["warnings"] == []
    assert parsed["published_start"] == "2026-10-03"
    assert parsed["published_end"] == "2026-10-30"
    assert parsed["suggested_export"] == {"start": "2026-10-01", "end": "2026-10-31"}
    first = parsed["rows"][0]
    assert len(first["post_id"]) == 19 and first["post_url"].endswith(first["post_id"])
    assert first["original_date"] == "Sat, Oct 3, 2026"
    assert parsed["sha256"] == parsed["file_sha256"]


def test_excel_resaved_export_gives_the_same_posts():
    original = parse_content_csv(ORIGINAL)
    excel = parse_content_csv(EXCEL)

    assert excel["rows"] == original["rows"]
    assert excel["repaired_post_ids"] == 10
    assert any("restored from the Post Link" in w for w in excel["warnings"])


def test_spreadsheet_number_without_a_link_is_refused_with_advice():
    rows = table(EXCEL)
    rows[1][3] = ""

    with pytest.raises(CsvValidationError, match="original file downloaded from X"):
        parse_content_csv(build(rows))


def test_spreadsheet_number_that_is_not_the_linked_post_is_refused():
    rows = table(EXCEL)
    rows[1][0] = "9.99999E+18"

    with pytest.raises(CsvValidationError, match="Row 2: invalid Post id"):
        parse_content_csv(build(rows))


def test_rounded_full_digits_are_restored_from_the_link():
    rows = table()
    exact = rows[1][0]
    # A number-formatted spreadsheet cell keeps 15 significant digits.
    rows[1][0] = str(int(Decimal(format(Decimal(exact), ".15g"))))
    assert rows[1][0] != exact

    parsed = parse_content_csv(build(rows))

    assert parsed["rows"][0]["post_id"] == exact
    assert parsed["repaired_post_ids"] == 1


def test_post_id_that_disagrees_with_its_link_is_refused():
    rows = table()
    rows[1][0] = rows[2][0]

    with pytest.raises(CsvValidationError, match="does not match the Post Link"):
        parse_content_csv(build(rows))


# ------------------------------------------------- separators and encodings

def test_semicolon_separated_file_is_read():
    parsed = parse_content_csv(build(table(), delimiter=";", terminator="\r\n"))

    assert parsed["rows"] == parse_content_csv(ORIGINAL)["rows"]


def test_utf16_tab_separated_file_is_read():
    raw = build(table(), delimiter="\t", terminator="\r\n", encoding="utf-16")

    assert raw.startswith((b"\xff\xfe", b"\xfe\xff"))
    assert parse_content_csv(raw)["rows"] == parse_content_csv(ORIGINAL)["rows"]


def test_other_encodings_are_refused_with_advice():
    rows = [
        [cell.encode("ascii", "ignore").decode() for cell in row]
        for row in table()
    ]
    rows[1][2] = "Café notes"
    raw = build(rows, encoding="cp1252")

    with pytest.raises(CsvValidationError, match="CSV UTF-8"):
        parse_content_csv(raw)


def test_header_case_and_spacing_do_not_matter():
    rows = table()
    rows[0] = [f"  {name.upper()} " for name in rows[0]]

    assert parse_content_csv(build(rows))["rows"] == parse_content_csv(ORIGINAL)["rows"]


def test_missing_columns_explain_what_to_upload():
    with pytest.raises(CsvValidationError, match="Missing required columns: Post text.*CSV UTF-8"):
        parse_content_csv(b"Post id,Date\n1,2026-10-01\n")


@pytest.mark.parametrize("delimiter,value", [(",", "1,234"), (";", "1.234"), (",", "1 234")])
def test_grouped_thousands_are_read(delimiter, value):
    rows = table()
    rows[1][4] = value

    parsed = parse_content_csv(build(rows, delimiter=delimiter))

    assert parsed["rows"][0]["impressions"] == 1234


@pytest.mark.parametrize("value", ["12.5", "1,23", "1.2K", "-3"])
def test_other_number_formats_are_refused(value):
    rows = table()
    rows[1][4] = value

    with pytest.raises(CsvValidationError, match="Row 2: invalid Impressions"):
        parse_content_csv(build(rows))


# --------------------------------------------------------------- dates, text

def test_iso_timestamp_dates_use_their_date():
    rows = table()
    rows[1][1] = "2026-10-03 14:05:00"

    assert parse_content_csv(build(rows))["rows"][0]["published_date"] == "2026-10-03"


def test_unsupported_date_names_the_row():
    rows = table()
    rows[3][1] = "03.10.2026"

    with pytest.raises(CsvValidationError, match="Row 4: unsupported publication date"):
        parse_content_csv(build(rows))


def test_post_without_text_is_kept_and_reported():
    rows = table()
    rows[1][2] = ""

    parsed = parse_content_csv(build(rows))

    assert parsed["row_count"] == 10
    assert parsed["rows"][0]["post_text"] == ""
    assert "1 rows have no post text." in parsed["warnings"]


# ---------------------------------------------------------------- duplicates

def test_exact_duplicate_rows_are_merged_not_dropped_silently():
    rows = table()
    parsed = parse_content_csv(with_rows([rows[1], rows[2]]))

    assert parsed["row_count"] == 10
    assert parsed["duplicates_merged"] == 2
    assert parsed["duplicate_conflicts"] == []
    assert any("2 exact duplicate rows were merged" in w for w in parsed["warnings"])


def conflicting():
    rows = table()
    changed = list(rows[1])
    changed[4] = str(int(changed[4]) + 50)
    return with_rows([changed]), rows[1][0]


def test_conflicting_duplicates_wait_for_the_users_choice():
    raw, post_id = conflicting()

    parsed = parse_content_csv(raw)

    assert parsed["needs_duplicate_choice"] is True
    assert parsed["row_count"] == 10
    assert len(parsed["rows"]) == 9
    [group] = parsed["duplicate_conflicts"]
    assert group["post_id"] == post_id
    assert [row["row"] for row in group["rows"]] == [2, 12]
    assert group["rows"][1]["impressions"] == group["rows"][0]["impressions"] + 50


def test_chosen_row_is_imported_and_changes_the_upload_identity():
    raw, post_id = conflicting()

    first = parse_content_csv(raw, duplicate_choices={post_id: 2})
    second = parse_content_csv(raw, duplicate_choices={post_id: 12})

    assert first["needs_duplicate_choice"] is False
    assert len(first["rows"]) == 10
    chosen = {row["post_id"]: row for row in second["rows"]}[post_id]
    assert chosen["impressions"] == {r["post_id"]: r for r in first["rows"]}[post_id]["impressions"] + 50
    assert first["file_sha256"] == second["file_sha256"] == parse_content_csv(raw)["file_sha256"]
    assert len({first["sha256"], second["sha256"], first["file_sha256"]}) == 3


@pytest.mark.parametrize("choices", [{}, {"1": 2}, "x"])
def test_incomplete_or_foreign_choices_are_refused(choices):
    raw, post_id = conflicting()

    with pytest.raises(CsvValidationError):
        parse_content_csv(raw, duplicate_choices=choices)


def test_choice_of_an_unlisted_row_is_refused():
    raw, post_id = conflicting()

    with pytest.raises(CsvValidationError, match="Choose one of the listed rows"):
        parse_content_csv(raw, duplicate_choices={post_id: 3})


def test_too_many_conflicting_posts_ask_for_a_new_export():
    header = table()[0]
    base = table()[1]
    rows = [header]
    for index in range(MAX_CONFLICTS + 1):
        post_id = str(2041000000000000000 + index)
        for impressions in ("1", "2"):
            row = list(base)
            row[0], row[3], row[4] = post_id, f"https://x.com/example_writer/status/{post_id}", impressions
            rows.append(row)

    with pytest.raises(CsvValidationError, match="Export the period again"):
        parse_content_csv(build(rows))


def test_duplicate_choices_header_is_validated():
    assert parse_duplicate_choices("") is None
    assert parse_duplicate_choices('{"2041000000000000000": 12}') == {"2041000000000000000": 12}
    for raw in ["[1]", '{"abc": 2}', '{"1": "2"}', '{"1": 1}', "{", '{"1": true}']:
        with pytest.raises(CsvValidationError):
            parse_duplicate_choices(raw)
