"""Free workspace policy for the source edition. Authentication remains required upstream.

The legacy paid flag is a compatibility flag for unrestricted report features,
not a statement that a transaction occurred.
"""

def billing_entitlement(profile, config, *, now=None):
    return isinstance(profile, dict)

def plan_entitlements(profile, config, *, now=None):
    active = isinstance(profile, dict)
    return {"plan": "community" if active else None, "paid": active, "trial": False,
            "active": active, "client_limit": 200 if active else 0,
            "case_limit": None, "allow_custom_branding": active}

def can_publish_unmarked_copy(profile, config, *, now=None):
    return isinstance(profile, dict)
