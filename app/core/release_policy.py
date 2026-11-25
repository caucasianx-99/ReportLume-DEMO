"""Fail-closed release checks; configuration is not proof of acceptance."""

from urllib.parse import urlsplit


def _approved(config, name: str) -> bool:
    return getattr(config, name, False) is True


def _https_origin(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    )


def _adequate_secret(value) -> bool:
    return isinstance(value, str) and len(value) >= 32


def _contact_email(value) -> bool:
    """A public, plain address: one @, a dotted domain, no spaces."""
    if not isinstance(value, str):
        return False
    local, _, domain = value.partition("@")
    return bool(
        3 <= len(value) <= 254
        and local
        and "@" not in domain
        and "." in domain.strip(".")
        and not any(char.isspace() for char in value)
    )


def _filled(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def customer_data_blockers(config) -> tuple[str, ...]:
    """Production customer data requires separately recorded evidence."""
    if getattr(config, "production", None) is False:
        return ()

    checks = (
        ("HTTPS public origin", _https_origin(getattr(config, "base_url", ""))),
        ("Firebase server configuration", bool(getattr(config, "firebase_ready", False))),
        ("CSV preview signing secret", _adequate_secret(getattr(config, "import_preview_secret", ""))),
        ("public contact e-mail", _contact_email(getattr(config, "contact_email", ""))),
        ("seller name", _filled(getattr(config, "seller_name", ""))),
        ("seller country", _filled(getattr(config, "seller_country", ""))),
        ("approved legal terms", _approved(config, "legal_review_complete")),
        ("approved privacy notice", _approved(config, "privacy_review_complete")),
        ("security acceptance", _approved(config, "security_qa_complete")),
        ("data lifecycle acceptance", _approved(config, "data_lifecycle_qa_complete")),
        ("verified single instance and worker", _approved(config, "railway_single_instance_verified")),
        ("verified client IP forwarding", _approved(config, "railway_client_ip_verified")),
        ("trusted proxy header explicitly enabled", _approved(config, "trust_railway_real_ip")),
        ("private auth rate-limit secret", _adequate_secret(getattr(config, "auth_rate_limit_key_secret", ""))),
    )
    return tuple(label for label, passed in checks if not passed)


def customer_data_ready(config) -> bool:
    return not customer_data_blockers(config)


def account_creation_blockers(config) -> tuple[str, ...]:
    """New accounts and clients collect personal data; production waits for
    the same evidence as customer CSV imports (prelaunch mode)."""
    return customer_data_blockers(config)


def account_creation_ready(config) -> bool:
    return not account_creation_blockers(config)
