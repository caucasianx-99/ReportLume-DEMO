# Source edition changes

Removed provider adapters, webhook/checkout routes, billing scripts, pricing UI, provider CI and commercial operations documents. Added free workspace policy and updated sample and public pages. Retained Firebase authentication, owner-scoped Firestore, reports, CSV parser, classification, PDFs and private links. Existing paid/trial schema identifiers remain for compatibility; use a fresh Firebase project. The archived commercial deployment must not be connected to this code.

Validation: Python compilation and selected offline CSV/parser and sample-rendering tests. Full authenticated integration and actual PDF generation were not verified in this environment. Former commercial tests and deployment acceptance suites were not carried into this edition; retained offline tests and a source-edition smoke test are included.

## Anonymous free edition

- Removed sign-in, signup, password reset, Firebase setup, and account deletion scripts.
- Added isolated anonymous browser workspaces backed by SQLite, with CSRF-protected creation and confirmed deletion.
- Preserved CSV import, monthly reporting, PDF output, branding, classification, and existing share/report services.
- Removed cloud authentication dependencies and replaced Firestore network calls with local compatibility storage.
- Seven-day retention and bounded storage are safety limits, not a trial. No paid features.
- Removed files: `firebase.json`, `firestore.rules`, `app/core/firebase.py`, `app/scripts/delete_account.py`, `app/templates/auth.html`.
- Production requires separate operational and security review. No live deployment was performed.
