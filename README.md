# Outage Explorer

Outage Explorer is a full-stack application built for the Arkham Technologies technical challenge. It lets users explore U.S. nuclear outage data from the EIA at three levels: national, facility, and generator.

The project includes a data extraction and refresh pipeline, a validated Parquet data model, role-based APIs, reconciliation tools, a restricted SQL explorer, and a React interface.

**Recommended starting point: the read-only demo.** It uses recorded EIA observations from September 22 to October 21, 2026, allowing reviewers to explore the application without configuring an API key or making live EIA requests. The demo includes the same analytical capabilities and role-based access controls as the production application, while keeping its data and refresh operations isolated.

## What you can do

| Feature | Viewer | Analyst | Admin |
| --- | --- | --- | --- |
| Browse national data and view the fleet offline share | Yes | Yes | Yes |
| Browse facility and generator data, with filters and pagination | No | Yes | Yes |
| Run restricted read-only SQL queries | No | Yes | Yes |
| Compare national, facility, and generator totals | No | Yes | Yes |
| Start a refresh and check its status | No | No | Yes |

Permissions are enforced by the backend, not just by hiding navigation items. Unauthenticated requests return `401`; authenticated users without permission receive `403`.

The **fleet offline share** is calculated as `national outage MW / national capacity MW × 100`. The interface displays daily values in a chart and table. Missing or invalid values are shown as unavailable rather than zero.

## Architecture

- **Backend:** Python 3.12, FastAPI, Pydantic, HTTPX, DuckDB, PyArrow/Parquet, and SQLGlot.
- **Frontend:** React, TypeScript, Vite, Tailwind CSS, locally owned shadcn/ui components, TanStack Query, and TanStack Table. The fleet chart uses SVG.
- **Tests:** Pytest for the backend; Vitest and Testing Library for the frontend.

The production data flow is:

```text
EIA API
  → extraction and validation
  → staged Parquet files for all three routes
  → atomic publication of one immutable snapshot
  → DuckDB analytical views
  → role-authorized FastAPI endpoints
  → React frontend
```

The demo uses the same analytical views and API authorization rules, but starts from recorded evidence and runs through a separate backend entry point. It cannot publish a production snapshot or change production watermarks and audits.

The [data model and ER diagram](docs/DATA_MODEL.md#logical-er-diagram) explain the grains, keys, types, and relationships. No external database, identity provider, or job broker is required.

## Requirements

- Python **3.12** (`>=3.12,<3.13`)
- Node.js **20.9+** and npm **10+**; a supported Node LTS release is recommended
- A local POSIX filesystem for the production pipeline's file locks, atomic renames, and directory sync operations

Windows, network filesystems, and object storage have not been validated for production publication.

Unless a section explicitly says otherwise, run all commands from the **repository root**. Backend and frontend configuration files live inside their respective app directories; there is no root `.env`. Never put secrets in `VITE_*` variables, which are exposed to the browser. The documented backend module is `outage_explorer.main:app` for production and `outage_explorer.demo:app` for the demo (not `outage_explorer.api.main:app`).

## Quick start: read-only demo

This is the easiest way to explore the application. It does **not** need an EIA API key or an internet connection to EIA.

### 1. Start the backend

```sh
python3.12 -m venv apps/backend/.venv
apps/backend/.venv/bin/python -m pip install -e './apps/backend[dev]'
test -e apps/backend/.env || cp apps/backend/.env.example apps/backend/.env

# First-time setup: choose passwords for viewer, analyst, and admin.
apps/backend/.venv/bin/python -m outage_explorer.seed_auth

# Prepare the isolated demo dataset from saved evidence.
apps/backend/.venv/bin/python -m outage_explorer.demo_data

apps/backend/.venv/bin/python -m uvicorn outage_explorer.demo:app \
  --host 127.0.0.1 --port 8000
```

Run `seed_auth` only once for an existing authentication configuration: it intentionally refuses to overwrite credentials. Re-running `demo_data` is safe when the existing artifacts are valid and unchanged; corrupt artifacts are rejected rather than silently replaced.

### 2. Start the frontend

In a second terminal:

```sh
npm ci
test -e apps/frontend/.env || cp apps/frontend/.env.example apps/frontend/.env
VITE_DEMO_MODE=true VITE_API_BASE_URL=/api npm run dev
```

Open the URL printed by Vite, usually **http://localhost:5173**. The development proxy forwards `/api` requests to port 8000. FastAPI documentation is available at **http://127.0.0.1:8000/docs**.

### 3. Sign in and explore

The demo has three accounts:

| Username | Role | What to try |
| --- | --- | --- |
| `viewer` | Viewer | National datasets and Fleet offline share |
| `analyst` | Analyst | All datasets, SQL Explorer, and Reconciliation |
| `admin` | Admin | Analyst features plus Refresh job tracking |

**Passwords are not included in the repository.** The `seed_auth` command prompts you to create them and stores salted PBKDF2 password hashes and a random signing key in the Git-ignored backend `.env`.

As an Analyst or Admin, try this supported SQL query:

```sql
SELECT outage_date, outage_mw
FROM national_outages
ORDER BY outage_date DESC
LIMIT 10
```

The SQL Explorer intentionally supports a limited subset: one `SELECT` from one authorized dataset, with named columns or `*`, supported filters, ordering, and a row limit. It rejects joins, subqueries, functions, aliases, aggregation, writes, and external file access. The API returns at most 200 rows. See [DECISIONS.md](DECISIONS.md) for the reasoning behind this restriction.

**Expected Admin demo behavior:** starting Refresh creates a job that ends in a clearly labeled read-only failure. This is intentional: the demo does not contact EIA, change a watermark, or pretend that data was published. `VITE_DEMO_MODE` only controls frontend presentation; the separate `outage_explorer.demo:app` backend enforces demo isolation.

## Authentication and configuration

The environment templates are:

- [Backend `.env.example`](apps/backend/.env.example): `AUTH_SIGNING_KEY`, `AUTH_USERS_JSON`, `AUTH_TOKEN_SECONDS` (default: 600), and optional `EIA_API_KEY` for live ingestion.
- [Frontend `.env.example`](apps/frontend/.env.example): `VITE_API_BASE_URL` and `VITE_DEMO_MODE`.

Restart the backend after changing authentication settings, or Vite after changing frontend settings. Exported backend environment variables take precedence over `.env` values; interpolation is disabled. Keep any existing EIA key when running authentication setup.

Login uses `POST /auth/login`, followed by `GET /auth/me`. Tokens are kept in memory, so reloading the page or logging out requires signing in again. Expired tokens and authenticated `401` responses clear the session and query cache.

This authentication setup is intended for the local challenge. Before public deployment, it would need HTTPS and deployment-level login rate limiting.

## Running tests

After installing the dependencies, run these commands from the repository root:

```sh
apps/backend/.venv/bin/python -m pytest apps/backend/tests
npm test
npm run typecheck
npm run build
```

Backend tests cover validation, ingestion and recovery, authorization, SQL restrictions, reconciliation, and recorded EIA evidence. Frontend tests use jsdom and mocked HTTP. These tests do not require a live EIA connection or API key.

I also tested the application manually in a browser, including the latest changes, using the demo and the non-demo frontend connected to a local production backend with a published live-EIA snapshot. The three roles, dataset access, pagination, SQL Explorer, and Admin refresh status were checked. These manual checks are separate from the automated suites; there is no automated browser end-to-end test suite. See [NOTES.md](NOTES.md) for verification details and AI assistance disclosure.

## Production ingestion and refresh

The production pipeline publishes internally validated observations using the **`facility_terminal_cross_grain_v1`** policy. Recorded-response replay successfully published the 30-day window through both extraction and incremental refresh.

A live EIA smoke test on October 23, 2026, fetched observations for **October 20, 2026** and published all three routes: **1 National, 55 Facility, and 95 Generator** rows. The published snapshot, watermarks, validation metadata, and run audit were checked. A second live refresh for the same date published a new immutable snapshot version with identical logical rows and unchanged watermarks. The Admin refresh endpoint and the browser's Admin refresh flow were also checked with a successful **no-op** request (no new data to fetch). These checks were performed against a local, isolated data directory, not a deployed service. **EIA source completeness remains unverified.**

### Run the production backend locally

Before fetching live data, add a valid EIA API key to `apps/backend/.env` (created during the demo setup):

```dotenv
EIA_API_KEY=your_eia_api_key_here
```

The key is not included in the repository. The production backend can start without it, but live ingestion and refresh require a valid key. A fresh checkout also has no published production snapshot, so data endpoints remain unavailable until the first successful publication. Keep the key in backend configuration only; never put it in a `VITE_*` variable or commit `.env`.

Stop the demo backend before starting the production backend on the same port. From the **repository root**, run:

```sh
apps/backend/.venv/bin/python -m uvicorn outage_explorer.main:app \
  --host 127.0.0.1 --port 8000
```

In a second terminal, from the **repository root**, start the frontend in non-demo mode:

```sh
VITE_DEMO_MODE=false VITE_API_BASE_URL=/api npm run dev
```

Open the Vite URL (usually `http://localhost:5173`) and sign in with the credentials created by `seed_auth`. The Vite development proxy forwards `/api` to **`http://127.0.0.1:8000`**. Keep `VITE_API_BASE_URL=/api` when using this proxy; pointing the browser directly at a different backend port bypasses the proxy and can trigger a CORS preflight failure. This is a local development setup connected to the production application entry point, not a public deployment.

Production reads only snapshots published under `data/eia/incremental`, **relative to the backend process's working directory**. When launched from the repository root, that means `<repository-root>/data/eia/incremental`. It never falls back to demo data, and analytical endpoints return `503` until a valid published snapshot is available. A fresh checkout will therefore need an initial publication before production data can be browsed.

### Publish or refresh data

There are **two alternative ways** to publish or refresh production data: use the Admin interface (or its API), **or** run the command-line pipeline. **Choose one; you do not need to run both.** When run from the repository root with the documented paths, both publish to the same production data directory and use the same ingestion and validation logic.

**Option A — Admin interface:** Sign in as `admin`, open **Refresh**, and enter a **First publication start date** and **Refresh through** date for the initial publication. Submit the job and wait for its status to show success. On subsequent refreshes, leave **First publication start date** empty: the pipeline derives its starting point from published watermarks. An explicit start date with an existing snapshot is rejected without modifying the published data. An end date equal to the current watermark with zero overlap produces a successful `no_op` without fetching new data. The same operation is available through `POST /refresh`, with status at `GET /refresh/{job_id}`.

**Option B — Command line:** For an explicitly authorized live run, configure `EIA_API_KEY` in `apps/backend/.env` or the environment. To perform a small **first publication** from the repository root, for example for October 20, 2026, use:

```sh
apps/backend/.venv/bin/python -m outage_explorer.refresh_eia \
  --env-file apps/backend/.env \
  --output-dir data/eia/incremental \
  --start 2026-10-20 \
  --end 2026-10-20 \
  --page-size 50 \
  --max-pages 4 \
  --attempts 2 \
  --backoff 1 \
  --timeout 20
```

This example makes live EIA requests, requires a valid key, and is intended only for a small verification window; the API may change or reject requests. Check for `refresh_published` and inspect the audit. Do not assume that a successful request establishes complete source coverage. The runbook describes larger windows, incremental refreshes, and failure recovery.

Refresh jobs run asynchronously inside a **single API worker**; job tracking is lost when that process restarts, although pipeline audits are stored on disk.

The pipeline validates each route, retries bounded transient failures, merges overlapping dates using published watermarks, and publishes all three routes together. A failure before publication leaves the current snapshot unchanged. National and Generator still require reported/received count equality. Facility requires an explicit terminal empty page, stable totals, valid records and unique keys, every requested date, and exact cross-grain key/MW checks. A Facility count mismatch is accepted only when its reported total equals the validated Generator count. The original reported count is retained, not rewritten. Manifests, audits, and API warnings disclose unverified source coverage.

The [ingestion and recovery runbook](docs/EIA_INCREMENTAL_REFRESH.md) documents CLI commands, conflict handling, audits, and filesystem guarantees. Demo data is isolated under Git-ignored `data/eia-demo` and cannot be adopted as a production snapshot.

## Assumptions and known limitations

- **EIA facility pagination:** for the recorded 30-day window, the facility route reports 2,850 rows but returns 1,650 unique facility-day observations, followed by an empty page. The cause is unresolved. Matching totals across grains do not independently establish complete source coverage.
- **Findings:** the three reproducible findings are one API count inconsistency and two unusual observations. They are not presented as three confirmed errors in the source data, and the statistical observations apply only to the recorded 30-day window.
- **Source identity and history:** natural keys are provisional outside the tested window. Offset pagination does not provide a consistent source snapshot, and corrections older than the configured refresh overlap are not automatically revisited.
- **Scale:** cumulative refresh and full snapshot validation use memory and work proportional to stored history. Separate HTTP requests can observe different published versions.
- **SQL execution:** the parser restricts supported statements and caps returned rows, but there is no hard query execution timeout.
- **Refresh operations:** jobs are in-process and not restart-durable. Distributed scheduling, automatic schema migration, and snapshot garbage collection are not implemented. Durability also depends on filesystem support for the required sync operations.
- **Network and dependencies:** intermittent EIA timeouts remain unexplained. Python dependencies use bounded version ranges rather than a lockfile. The [dated setup review](docs/setup-review.md) records historical development-tool security advisories, not a current security audit.

## Project documentation

| File | Contents |
| --- | --- |
| [DECISIONS.md](DECISIONS.md) | Seven engineering decisions, rejected alternatives, and reasons |
| [FINDINGS.md](FINDINGS.md) | Reconciliation results, three classified findings, and reproduction commands |
| [NOTES.md](NOTES.md) | AI assistance, a concrete AI-generated mistake, review, and verification |
| [Data model and ER diagram](docs/DATA_MODEL.md#logical-er-diagram) | Analytical views, types, keys, and relationships |
| [Incremental refresh runbook](docs/EIA_INCREMENTAL_REFRESH.md) | Extraction, publication, recovery, and audits |
| [EIA schema exploration](docs/EIA_SCHEMA.md) | Observed source fields and validation assumptions |
| [Facility pagination investigation](docs/EIA_PAGINATION_INVESTIGATION.md) | Recorded requests and evidence behind the unresolved count discrepancy |

The repository includes incremental Git history, automated tests, and app-specific `.env.example` files. [AGENTS.md](AGENTS.md) contains the development guidelines used during implementation.
