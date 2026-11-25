# Reportlume — Free Edition

Free, self-hosted X Content CSV analytics and monthly report application. No registration, login, subscription, payment, Firebase, or external database is required. Import authorized CSV exports, manage clients, classify posts, generate monthly reports, export PDF and share private review links. The fictional sample remains available.

## Local setup (Windows PowerShell)

Python 3.12 recommended. Install WeasyPrint's system dependencies if PDF rendering is unavailable.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Open http://127.0.0.1:8001/ and select **Open free workspace**. No `.env` is needed for local use. The example report is at `/sample`; the health check is at `/health`.

## Data and privacy

- An anonymous, random browser cookie grants access to one workspace; no email or password is collected. The public share identifier is not the owner credential. Keep the cookie private.
- SQLite defaults to `~/.reportlume/workspace.sqlite3`. Set `REPORTLUME_DB` to a private writable file path to change it. No cloud service is contacted by the storage backend.
- The workspace expires seven days after creation (data retention, not a paid trial). Expired workspaces become inaccessible and are deleted when another workspace is created. Export data and PDFs before expiry. Clearing cookies loses access. The dashboard can delete the entire workspace, including share links, immediately at the application level. SQLite deletion is not forensic disk erasure.
- Limits: 500 live workspaces per instance, 30,000 documents and 64 MiB of serialized document data per workspace, plus the existing CSV and reporting safety limits. These are resource protections, not paid plans.
- For local evaluation use the loopback host. Do not expose development mode to the internet. For public hosting, configure HTTPS, `APP_ENV=production`, `BASE_URL`, a random `IMPORT_PREVIEW_SECRET` (32+ characters), operator identity/contact and actual legal/privacy/security/data-lifecycle approvals; existing production gates refuse new workspaces/imports until approved. Deploy a persistent private database volume, one worker, request/resource rate limits at the edge, backups and periodic expired-record cleanup. Public hosting has not been security/load audited. Share URLs use `BASE_URL`; set it to the actual deployment origin.
- Do not commit credentials, the database, private exports, or `.env`. Only process authorized analytics. Independent of X; no scraping or automatic posting. X controls analytics export availability.

## Tests

`python -m pip install -r requirements-dev.txt` then `python -m pytest -q`.

This repository is a portfolio source release. No open-source license has been selected by the author; public visibility alone does not grant reuse rights.
