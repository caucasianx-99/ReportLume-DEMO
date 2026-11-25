"""Offline security, reporting and payment-boundary checks."""

from pathlib import Path

from app.services.reporting import (
    safe_post_url,
    _quality,
)


ROOT = Path(__file__).resolve().parents[1]


def test_x_post_link_matches_id():
    result = safe_post_url(
        "https://x.com/example/status/123",
        "123",
    )

    assert result == (
        "https://x.com/example/status/123"
    )


def test_malicious_or_mismatched_links_are_not_exposed():
    unsafe_urls = (
        "javascript:alert(1)",
        "https://example.com/example/status/123",
        "https://x.com/example/status/456",
        "https://x.com@example.com/example/status/123",
        "http://x.com/example/status/123",
        "https://x.com/example/status/123?redirect=1",
    )

    for value in unsafe_urls:
        assert safe_post_url(value, "123") == ""


def test_insufficient_quality_stays_not_comparable():
    result = _quality([], "2026-10")

    assert result["comparable_cohort"] is False


def test_provider_implementations_are_not_in_core():
    """Product-domain modules must not parse provider-specific events."""
    domain_paths = (
        "app/core/config.py",
        "app/services/workspace_access.py",
    )

    forbidden = (
        "lemon_",
        "lemonsqueezy",
        "store.lemonsqueezy",
    )

    for relative in domain_paths:
        content = (
            ROOT / relative
        ).read_text(
            encoding="utf-8"
        ).lower()

        for token in forbidden:
            assert token not in content, (
                relative,
                token,
            )


def test_obsolete_payment_modules_are_absent():
    obsolete = (
        "app/services/payment_selection.py",
        "app/services/checkout_intent_policy.py",
        "app/services/billing_transition_policy.py",
    )

    for relative in obsolete:
        assert not (
            ROOT / relative
        ).exists(), relative


def test_application_has_no_imports_from_removed_modules():
    obsolete_imports = (
        "from app.services.payment_selection import",
        "from app.services.checkout_intent_policy import",
        "from app.services.billing_transition_policy import",
        "import app.services.payment_selection",
        "import app.services.checkout_intent_policy",
        "import app.services.billing_transition_policy",
    )

    for path in (ROOT / "app").rglob("*.py"):
        content = path.read_text(
            encoding="utf-8"
        )

        for expression in obsolete_imports:
            assert expression not in content, (
                path.relative_to(ROOT),
                expression,
            )
