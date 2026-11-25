# GitHub Pages browser edition

This additive patch leaves the existing Python Free Edition intact. The independent `docs/` site is a client-side subset, with a matching visual theme. No API keys, registration, payment, server or build process.

## Publish

Copy `docs/`, `tests/pages-core.test.cjs`, and this file into the repository root. Commit and push. In Settings → Pages choose Deploy from a branch → main → /docs → Save. Default project URL: `https://caucasianx-99.github.io/ReportLume-DEMO/` (once GitHub reports successful deployment).

## Local preview

From the repository root run `python -m http.server 8003 --bind 127.0.0.1 --directory docs` and visit `http://127.0.0.1:8003/`. Use the built-in fictional example or create a client and import an authorized CSV. Run `node --test tests/pages-core.test.cjs` with Node 18+ for parser tests.

## Scope / limits

- Local IndexedDB storage, 30 clients, 10,000 content rows per client, 10 MB per file. Browser quota applies; deleting site data clears records. No transfer of the Python workspace. Do not use on a shared browser profile. Other Pages projects on the same origin can access origin-scoped storage; the portfolio demo is best evaluated with fictional data. For private data use the localhost edition or an isolated origin you control.
- X CSV parsing handles quoted/multiline fields, comma/semicolon/tab delimiters, UTF-8 or BOM-marked UTF-16, IDs, dates and optional metrics. Exact duplicate rows merge; conflicting rows in a single export are rejected. Reimports update a post's metric snapshot and retain classification; conflicting date/text are rejected. Missing metrics remain unavailable. No full parity with Python's importer or duplicate-choice workflow.
- Client management, monthly selection, authorship and post/reply classification, goals and notes, summary metrics, top posts and print-ready report. Save PDF via the browser print dialog. Not the original server-rendered PDF generator. No private review URL, client approvals, white-label branding, case documents, topic/format classification, or server-side synchronization. No automatic data expiry. No claim of full backend feature parity.
- No external scripts or network calls containing imported data. The fictional sample is served from the same site.
- Tests: JS syntax and eight Node core tests passed. The managed browser blocked local navigation, so interactive UI, IndexedDB and PDF printing must still be checked manually. GitHub deployment has not been performed or verified here.
