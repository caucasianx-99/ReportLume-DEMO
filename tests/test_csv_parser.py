
"""Synthetic CSV tests without live Firebase."""

import csv
import io

import pytest

from app.services.x_content_csv import (
    COLUMNS,
    CsvValidationError,
    ENGAGEMENT_PARTS,
    parse_content_csv,
)


def fixture_csv(**changes):
    row = {
        "Post id": "123456789012345678901234567890",
        "Date": "Wed, Jan 3, 2024",
        "Post text": "@someone Example &amp; note",
        "Post Link": (
            "https://x.com/demo/status/123456789012345678901234567890"
        ),
        **{
            key: "0"
            for key in COLUMNS
            if key not in (
                "Post id",
                "Date",
                "Post text",
                "Post Link",
            )
        },
    }

    row.update(changes)

    handle = io.StringIO()

    writer = csv.DictWriter(
        handle,
        fieldnames=list(COLUMNS),
    )

    writer.writeheader()
    writer.writerow(row)

    return handle.getvalue().encode("utf-8")


def test_real_17_column_shape_with_synthetic_values():
    parsed = parse_content_csv(
        fixture_csv()
    )

    assert len(parsed["columns"]) == 17
    assert len(ENGAGEMENT_PARTS) == 11
    assert parsed["row_count"] == 1
    assert parsed["engagement_mismatches"] == 0


def test_big_post_id_text_html_entity_and_zero():
    row = parse_content_csv(
        fixture_csv()
    )["rows"][0]

    assert row["post_id"] == (
        "123456789012345678901234567890"
    )

    assert row["post_text"] == (
        "@someone Example & note"
    )

    assert row["impressions"] == 0


def test_missing_metric_is_null_not_zero():
    row = parse_content_csv(
        fixture_csv(
            **{"Impressions": ""}
        )
    )["rows"][0]

    assert row["impressions"] is None


def test_engagement_mismatch_warns_but_preserves_data():
    parsed = parse_content_csv(
        fixture_csv(
            **{"Engagements": "100"}
        )
    )

    assert parsed["engagement_mismatches"] == 1
    assert parsed["rows"][0]["engagements"] == 100


def test_at_prefix_is_only_possible_reply():
    parsed = parse_content_csv(
        fixture_csv()
    )

    assert parsed["possible_replies"] == 1

    assert "authorship" not in parsed["rows"][0]


def test_invalid_date_rejected():
    with pytest.raises(CsvValidationError):
        parse_content_csv(
            fixture_csv(
                **{"Date": "Mon, Jan 3, 2024"}
            )
        )


def test_bad_url_rejected():
    with pytest.raises(CsvValidationError):
        parse_content_csv(
            fixture_csv(
                **{
                    "Post Link":
                        "javascript:alert(1)"
                }
            )
        )


def test_bom_supported():
    raw = fixture_csv()

    assert parse_content_csv(
        b"\xef\xbb\xbf" + raw
    )["row_count"] == 1


def test_identical_duplicate_rows_are_merged_and_reported():
    raw = fixture_csv()

    header, row = raw.split(
        b"\r\n",
        1,
    )

    parsed = parse_content_csv(
        header + b"\r\n" + row + row
    )

    assert parsed["row_count"] == 1
    assert len(parsed["rows"]) == 1
    assert parsed["duplicates_merged"] == 1
    assert any("duplicate" in warning for warning in parsed["warnings"])


def test_filename_is_hint_not_publication_range():
    parsed = parse_content_csv(
        fixture_csv(),
        "account_analytics_content_"
        "2024-01-01_2024-01-10.csv",
    )

    assert parsed["suggested_export"]["start"] == (
        "2024-01-01"
    )

    assert parsed["published_start"] == (
        "2024-01-03"
    )
