from pathlib import Path
from app.services.workspace_access import plan_entitlements, can_publish_unmarked_copy

def test_no_subscription_required():
    rights = plan_entitlements({}, None)
    assert rights["active"] and not rights["trial"]
    assert rights["client_limit"] == 200
    assert can_publish_unmarked_copy({}, None)
    assert not plan_entitlements(None, None)["active"]

def test_no_provider_modules():
    root = Path(__file__).resolve().parents[1]
    assert not list((root / "app/services").glob("payment_*.py"))
    assert not (root / "app/routes/billing.py").exists()

def test_public_pages():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    for path in ("/", "/sample", "/sample/workspace", "/health", "/ready"):
        assert c.get(path).status_code == 200
    assert c.get("/billing").status_code == 404
