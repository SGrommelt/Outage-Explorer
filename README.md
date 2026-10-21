# Outage Explorer

A minimal monorepo for an outage exploration application, with a liveness API, React starter page, manual EIA schema probe, and local date-window extraction command. Database-backed querying and outage analysis are not implemented.

## Layout

```text
apps/
  backend/
    src/outage_explorer/
      api/health.py       # GET /health
      main.py             # FastAPI app
      explore_eia.py      # Bounded schema probe
      extract_eia.py      # Validated Parquet snapshot extraction
    tests/                # Offline API and extraction tests
    pyproject.toml        # Python package and dependencies
  frontend/
    src/
      components/ui/      # Locally owned shadcn/ui components
      lib/                # Shared helpers
      test/               # Vitest setup
      App.tsx             # Starter page
    components.json       # shadcn/ui generator configuration
AGENTS.md                 # Engineering guidelines
package.json              # npm workspace and frontend commands
```

## Requirements

- Python **3.12** (the backend package targets `>=3.12,<3.13`).
- Node.js **20.9+** and npm **10+**; use a supported Node LTS release.

## Backend

From the repository root:

```sh
python3.12 -m venv apps/backend/.venv
source apps/backend/.venv/bin/activate
python -m pip install -e './apps/backend[dev]'
cp apps/backend/.env.example apps/backend/.env
python -m uvicorn outage_explorer.main:app --reload --app-dir apps/backend/src
```

The API runs at http://127.0.0.1:8000. `GET /health` returns `{"status":"ok"}`. Interactive API docs are at http://127.0.0.1:8000/docs. No environment settings are required or loaded by the backend yet.

FastAPI, DuckDB, HTTPX, Pandas, PyArrow, Pydantic, and SQLGlot are declared dependencies. SQLite is provided by Python's built-in `sqlite3` module. Uvicorn runs the development server; Pytest is a development extra. No database is initialized.

## Frontend

In a second terminal, from the repository root:

```sh
npm ci
cp apps/frontend/.env.example apps/frontend/.env
npm run dev
```

Open the URL printed by Vite (normally http://localhost:5173). The development proxy forwards `/api/*` to the local backend, so `/api/health` reaches `/health`. `VITE_API_BASE_URL` reserves the API base URL for future frontend requests; the starter page makes no requests. For production, configure an equivalent reverse proxy or the future client's API base URL.

React, TypeScript, Vite, Tailwind CSS, TanStack Query, TanStack Table, Monaco Editor, and Recharts are configured or installed. QueryClientProvider is wired at the entry point. Tables, editor, and charts remain unused until features require them. shadcn/ui uses source-owned components rather than a runtime package; `components.json`, aliases, and the `cn` helper are ready for generating components into `src/components/ui`.

## Checks

```sh
# From the repository root
npm test
npm run typecheck
npm run build

# With the backend virtual environment activated
cd apps/backend
python -m pytest
```

Use `npm run test:watch --workspace @outage-explorer/frontend` for interactive frontend tests. `npm run preview --workspace @outage-explorer/frontend` serves a completed frontend build locally.

Environment files, local data, build outputs, and dependencies are ignored by Git. Commit `package-lock.json` with dependency changes. Python dependencies use bounded ranges in `pyproject.toml`; there is no Python lockfile yet.

## Manual EIA schema exploration

Configure `EIA_API_KEY` in `apps/backend/.env` or export it in your shell. The exploration tool uses python-dotenv to load that specific file without overriding exported values or interpolating variables. The FastAPI application does not load this key or make EIA requests.

From the repository root, with backend dependencies installed:

```sh
apps/backend/.venv/bin/python -m outage_explorer.explore_eia --output data/eia-exploration.json
```

The tool requests metadata and at most two three-row pages for each of the national, facility, and generator routes. It discovers measures and identifier facets from metadata, requests daily observations within seven days of the advertised end date, and sorts by period then identifiers. `--length` accepts 1–10 rows per page; `--timeout` accepts 1–60 seconds per HTTP operation (default 45). There are no automatic retries, backfills, or database writes.

The JSON report includes metadata, sampled rows, field types, missing/null counts, date parsing, pagination information, and candidate-key checks within the sample. It removes request echoes and redacts the key; never enable HTTP request logging for credential-bearing URLs. Without `--output`, the sanitized report is printed. Failures produce a nonzero exit status; partial results retain successful metadata/pages. Local reports under `data/` are ignored by Git.

See [docs/EIA_SCHEMA.md](docs/EIA_SCHEMA.md) for verified observations and limitations. Offline exploration tests use HTTPX MockTransport and never load the real `.env` file:

```sh
apps/backend/.venv/bin/python -m pytest apps/backend/tests
```

## Local EIA extraction

With the backend installed and `EIA_API_KEY` configured as above, run from the repository root:

```sh
apps/backend/.venv/bin/python -m outage_explorer.extract_eia \
  --start 2026-10-20 --end 2026-10-20 --page-size 25
```

Dates are inclusive and required. The command extracts national, facility, and generator rows with bounded pages, deterministic sorting, validation, and bounded exponential retries. It stages typed Parquet files and publishes a complete three-route snapshot through `data/eia/<start>_<end>/current.json`. Repeat runs replace the current snapshot pointer rather than appending records. Readers must resolve that pointer once and use that immutable run; do not glob across runs or overlapping date windows.

**Live validation found an upstream completeness issue:** for October 20, EIA returned 55 facility rows but advertised 95. National (1 row) and generator (95 rows) validated. The command correctly exits 1 and publishes nothing for this window. This limitation is not bypassed, and no full historical extraction has been run.

See [docs/EIA_EXTRACTION.md](docs/EIA_EXTRACTION.md) for output layout, schemas, retry options, failure guarantees, live results, and limitations.

The [completed pagination investigation](docs/EIA_PAGINATION_INVESTIGATION.md) independently reproduces the facility mismatch and distinguishes verified evidence from unresolved hypotheses. Its recorded-response regression runs offline; validation has not been weakened.

## Incremental refresh

The cumulative refresh command uses a dedicated current snapshot, per-route watermarks, and durable ingestion audits. From the repository root:

```sh
# First run (explicit starting window)
apps/backend/.venv/bin/python -m outage_explorer.refresh_eia --start 2026-10-18 --end 2026-10-20
# Subsequent run (start derived from the last successful publication)
apps/backend/.venv/bin/python -m outage_explorer.refresh_eia --end 2026-10-21 --overlap-days 2
```

Output defaults to `data/eia/incremental`. Identical overlap rows merge once; conflicting values fail unless `--conflict-policy replace` is explicitly selected. All three datasets and watermarks publish through one atomic pointer update. Failed attempts retain audit metadata without advancing watermarks. The known facility total discrepancy still blocks publication; no completeness bypass was added and no live refresh was run.

See [docs/EIA_INCREMENTAL_REFRESH.md](docs/EIA_INCREMENTAL_REFRESH.md) for legacy snapshot adoption, replacement policy, output layout, recovery, and limits. The original extraction command retains its independent date-window behavior.

## Dependency setup notes

Vitest uses the patched 4.1 release line, compatible with the existing Vite 6 and Node 20 setup. Some npm 10 versions crash with `Cannot read properties of null (reading 'edgesOut')` when resolving its optional peer dependencies. Use `npm ci` with the supplied lockfile for normal setup. If updating dependencies triggers this resolver error, use temporary newer tools without changing global installations:

```sh
npm exec --yes --package=node@22 --package=npm@11 -- npm install
```

The Python 3.12 backend installs with its existing dependency ranges. The current Starlette test client emits a deprecation warning for HTTPX; the health smoke test still passes. HTTPX remains the declared client dependency.

See [the setup review](docs/setup-review.md) for validation results, remaining development dependency vulnerabilities, and remediation limits.
