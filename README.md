# Outage Explorer

## Overview

Outage Explorer is an Arkham Technologies take-home application for exploring EIA nuclear outages at national, facility, and generator grains.

**Start with the read-only real-data demo below.** It uses saved September 22–October 21, 2026 observations and needs no EIA API key. Source completeness is unverified; strict production publication remains blocked by an unresolved facility API count discrepancy. The requirement for three independent verified real anomalies is also incomplete.

## Features and roles

| Capability | Viewer | Analyst | Admin |
| --- | --- | --- | --- |
| National catalog, date-filtered previews, fleet metric | Yes | Yes | Yes |
| Facility/generator catalog and previews, facility filters | No | Yes | Yes |
| Restricted read-only SQL Explorer | No | Yes | Yes |
| Daily and facility-level reconciliation | No | Yes | Yes |
| Start refresh and inspect its job status | No | No | Yes |

Authorization is server-side, before data access. Unauthenticated requests receive 401; authenticated forbidden requests receive 403. Frontend navigation is only a convenience.

Fleet offline share is `national outage MW / national capacity MW × 100`. It loads the latest local observation period automatically, with a historical chart and table. Missing/invalid values remain unavailable, not zero. Matching reconciliation aggregates show internal consistency, not independently certified source completeness.

## Tech stack and architecture

- Backend: Python 3.12, FastAPI, DuckDB, PyArrow/Parquet, HTTPX, Pydantic, SQLGlot; Pytest.
- Frontend: React, TypeScript, Vite, Tailwind, locally owned shadcn/ui components, TanStack Query/Table; Vitest and Testing Library. The metric chart uses SVG.
- Production: EIA → validated staging → immutable three-route Parquet run → atomic published pointer → pinned DuckDB views → authorized API → browser.
- Demo: checked-in evidence → separate validated demo Parquet → the same views/API policies through an explicitly separate app. No production pointers, watermarks, or audits are changed.

See the [implemented model and ER diagram](docs/DATA_MODEL.md#logical-er-diagram). No external database, identity provider, or job broker is required.

## Prerequisites

- Python **3.12** (`>=3.12,<3.13`).
- Node.js **20.9+**, npm **10+**; use a supported Node LTS release.
- A local POSIX filesystem for production locking, atomic rename, and directory fsync. Windows and network/object storage are not validated.

Run the commands below from the repository root. Backend and frontend environment templates are app-scoped; do not create a root `.env`. Never put secrets in browser-visible `VITE_*` settings.

## Quick Start — working real-data demo

### Backend terminal

```sh
python3.12 -m venv apps/backend/.venv
apps/backend/.venv/bin/python -m pip install -e './apps/backend[dev]'
test -e apps/backend/.env || cp apps/backend/.env.example apps/backend/.env

# First-time setup only: choose passwords for all three development users.
apps/backend/.venv/bin/python -m outage_explorer.seed_auth

# Validate saved evidence and prepare the isolated demo; no network/key needed.
apps/backend/.venv/bin/python -m outage_explorer.demo_data
apps/backend/.venv/bin/python -m uvicorn outage_explorer.demo:app \
  --host 127.0.0.1 --port 8000
```

Skip `seed_auth` if authentication is already configured: it deliberately refuses to overwrite credentials. Demo preparation is idempotent for valid unchanged artifacts; it refuses corrupt existing artifacts rather than overwriting them.

### Frontend terminal

```sh
npm ci
test -e apps/frontend/.env || cp apps/frontend/.env.example apps/frontend/.env
VITE_DEMO_MODE=true VITE_API_BASE_URL=/api npm run dev
```

Open Vite's printed URL (normally http://localhost:5173). The `/api` proxy forwards to port 8000; API docs are at http://127.0.0.1:8000/docs and `/health` is public. For a hosted frontend, provide an equivalent same-origin reverse proxy; cross-origin CORS is not configured.

Sign in, inspect datasets, open Fleet offline share, and use Reconciliation as Analyst/Admin. An allowed SQL example is:

```sql
SELECT outage_date, outage_mw FROM national_outages ORDER BY outage_date DESC LIMIT 10
```

SQL supports one SELECT against one approved dataset, plain columns or `*`, basic predicates, ORDER BY, and LIMIT. Joins, subqueries, functions, aliases, aggregation, writes, and external/file access are rejected. Results are capped at 200 rows; see [decisions 5 and 6](DECISIONS.md#5-sql-is-a-compiled-deliberately-small-read-only-subset).

**Demo refresh deliberately fails:** Admin receives a job ID and can observe a final read-only failure. It does not contact EIA or simulate successful publication. The warning flag labels the frontend; it does not select the backend or grant access. Always use it with the separate demo backend entry point.

## Demo users and credential setup

The development-only usernames are **viewer**, **analyst**, and **admin**. There are no default passwords. `seed_auth` asks you to choose and confirm passwords, stores salted PBKDF2 hashes, and creates a random signing key in the Git-ignored backend `.env`. Passwords and secrets are not stored in source code.

- [Backend template](apps/backend/.env.example): `AUTH_SIGNING_KEY`, `AUTH_USERS_JSON`, `AUTH_TOKEN_SECONDS` (default 600), and optional production `EIA_API_KEY`.
- [Frontend template](apps/frontend/.env.example): `VITE_API_BASE_URL` and `VITE_DEMO_MODE` only.

Restart the backend after auth changes and Vite after frontend configuration changes. Exported backend settings override file values; interpolation is disabled. Keep any existing EIA key when configuring authentication. Login uses `POST /auth/login`, followed by `GET /auth/me`.

Tokens stay in memory: reload/logout requires another login. Expiry and authenticated 401 responses clear the session and query cache. This is a local challenge implementation; use HTTPS and deployment-level rate limits before exposing it outside a trusted environment.

## Running tests

From the repository root, after installing dependencies:

```sh
apps/backend/.venv/bin/python -m pytest apps/backend/tests
npm test
npm run typecheck
npm run build
```

Backend tests use synthetic Parquet, mocked HTTP, and recorded evidence; they do not need an EIA key or live requests. Frontend tests use jsdom/mock HTTP. These are not automated browser end-to-end tests. [Engineering Notes](NOTES.md) distinguish automated checks, user-reported browser verification, and attribution questions.

## Production ingestion and refresh

Stop the demo backend before starting the production app on the same port:

```sh
apps/backend/.venv/bin/python -m uvicorn outage_explorer.main:app \
  --host 127.0.0.1 --port 8000
```

Restart Vite with `VITE_DEMO_MODE=false VITE_API_BASE_URL=/api npm run dev`. Production reads only published snapshots at `data/eia/incremental`; it never falls back to demo data. Analytical endpoints return 503 until a valid snapshot exists.

For explicitly authorized live ingestion, configure `EIA_API_KEY` in `apps/backend/.env` or the environment. Admin-only `POST /refresh` starts an in-process job; `GET /refresh/{job_id}` reports pending/running/succeeded/failed. Use a **single API worker**. API job IDs are lost on restart, while production pipeline audits remain on disk.

Refresh reuses strict extraction, bounded retries, overlap merging, per-route watermarks, and atomic three-route publication. Failed pre-publication runs leave the current snapshot unchanged. **The recorded facility responses still fail completeness validation; no successful real production refresh is claimed.** CLI commands, conflict policy, recovery, and filesystem guarantees are in the [ingestion runbook](docs/EIA_INCREMENTAL_REFRESH.md).

Demo data stays under Git-ignored `data/eia-demo`; there is no production adoption path. Stop the demo and switch entry points to return to production; no production data needs restoration.

## Known limitations

- Facility responses report 2,850 records but return 1,650 unique facility-day rows for the saved window, then an empty terminal page. The cause and authoritative source coverage are unresolved. Related grains can share omissions even when MW totals match.
- The 30-day report is reproducible, but **three independent real anomalies have not been verified**. Normal capacity changes, rounding, and synthetic regressions are not counted as defects.
- Natural keys and source lifecycle/null/rating assumptions remain provisional across full history. Offset pagination has no source snapshot isolation; older corrections beyond refresh overlap are not automatically revisited.
- Full snapshot validation and cumulative refresh use memory/work proportional to history. SQL has a result cap, but no hard execution-time budget. Separate HTTP requests may observe different published versions.
- Refresh jobs are in-process, single-worker, and not restart-durable. Snapshot retention/garbage collection, distributed scheduling, and automatic schema migration are intentionally absent. Filesystem durability depends on the OS/device honoring fsync.
- Intermittent EIA timeouts remain unexplained. Fresh connections and bounded retries mitigate rather than resolve them.
- Python dependencies use bounded ranges without a lockfile. The dated [setup review](docs/setup-review.md) records outstanding development-tool advisories; its historical counts are not a fresh security audit.

## Documentation links

- [DECISIONS.md](DECISIONS.md) — seven choices, alternatives, and rationales.
- [FINDINGS.md](FINDINGS.md) — real observations, comparisons, anomaly shortfall, and offline reproduction.
- [NOTES.md](NOTES.md) — AI assistance, review/verification, and outstanding attribution confirmation.
- [Data model and ER diagram](docs/DATA_MODEL.md#logical-er-diagram) — exact types, keys, relationships, and validation.
- [Ingestion runbook](docs/EIA_INCREMENTAL_REFRESH.md) — independent windows, incremental refresh, audits, recovery.
- [Observed EIA schema](docs/EIA_SCHEMA.md) and [dated pagination investigation](docs/EIA_PAGINATION_INVESTIGATION.md) — supporting source evidence.

The repository retains incremental Git history; preserve it when submitting rather than delivering only a flattened source archive. [AGENTS.md](AGENTS.md) contains development guidelines.
