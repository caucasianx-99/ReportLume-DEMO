# Reportlume

Self-hosted source edition of an X Content CSV client reporting workspace. Import authorized exports, classify posts, manage clients, build monthly reports, export PDF and share private review links. The fictional sample workspace is included. No subscription, checkout or payment integrations are included.

## Run locally

Python 3.12 recommended. Install the system libraries required by WeasyPrint (see its installation documentation), then:

```bash
python -m venv .venv
# Activate your virtual environment
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000/sample and /sample/workspace for fictional demonstrations.

For real account workflows, create a **new** Firebase project, enable email/password authentication, create Firestore, deploy the included deny-all client rules, copy `.env.example` to `.env`, configure the project ID, web API key and server credentials outside the repository, and set independent random secrets (at least 32 characters). Verify your email after signup. Do not reuse the former commercial database or credentials. The previous Railway deployment is not required.

All authenticated accounts in the new source edition have access to report features, with a 200 active-client safety limit. Authentication and tenant isolation remain in place. Legacy internal paid/trial field names are retained for report schema compatibility; no payment is performed. The production release gates remain fail-closed; this release is intended for local evaluation and requires separate operational/security review before internet deployment.

Do not commit credentials or private exports. Only process authorized analytics. X export availability depends on X. The project is independent of X.

This is a portfolio source release, not an active hosted commercial service. No open-source license has been selected by the author; public visibility alone does not grant reuse rights.
