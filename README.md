# Outage Explorer

Outage Explorer is a full-stack application built for the Arkham Technologies technical challenge. It lets users explore U.S. nuclear outage data from the EIA at three levels: national, facility, and generator.

The project includes a data extraction and refresh pipeline, a validated Parquet data model, role-based APIs, reconciliation tools, a restricted SQL explorer, and a React interface.

**Recommended starting point: the read-only demo.** It runs on recorded EIA observations from September 22 to October 21, 2026, without an API key or live requests. The facility API reports a higher row count than it returns, so I have not treated this evidence as a complete production dataset or bypassed the ingestion checks. The discrepancy and two unusual patterns in the recorded data are documented in [FINDINGS.md](FINDINGS.md).

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

Run the following commands from the **repository root**. Backend and frontend configuration files live inside their respective app directories; there is no root `.env`. Never put secrets in `VITE_*` variables, which are exposed to the browser.

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

I also tested the application manually in a browser, including the latest changes. These manual checks are separate from the automated suites; there is no automated browser end-to-end test suite. See [NOTES.md](NOTES.md) for verification details and AI assistance disclosure.

## Production ingestion and refresh

The production pipeline is implemented, but **successful publication of the recorded real-data window is currently blocked by the EIA facility pagination discrepancy**. The commands below describe how the production application runs; they are not a claim that live ingestion has succeeded.

Stop the demo backend before starting the production backend on the same port:

```sh
apps/backend/.venv/bin/python -m uvicorn outage_explorer.main:app \
  --host 127.0.0.1 --port 8000
```

Start the frontend in production mode:

```sh
VITE_DEMO_MODE=false VITE_API_BASE_URL=/api npm run dev
```

Production reads only snapshots published under `data/eia/incremental`. It never falls back to demo data, and analytical endpoints return `503` until a valid published snapshot is available.

For an explicitly authorized live run, configure `EIA_API_KEY` in `apps/backend/.env` or the environment. An Admin can submit `POST /refresh` and poll `GET /refresh/{job_id}`. Refresh jobs run asynchronously inside a **single API worker**; job tracking is lost when that process restarts, although pipeline audits are stored on disk.

The pipeline validates each route, retries bounded transient failures, merges overlapping dates using published watermarks, and publishes all three routes together. A failure before publication leaves the current snapshot unchanged. I chose not to relax completeness validation simply to produce a successful-looking refresh.

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
