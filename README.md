# Outage Explorer

A minimal monorepo for an outage exploration application. This scaffold contains a liveness API and a React starter page; no outage ingestion, database schema, query execution, or other business logic is implemented.

## Layout

```text
apps/
  backend/
    src/outage_explorer/
      api/health.py       # GET /health
      main.py             # FastAPI app
    tests/                # Pytest smoke tests
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

## Dependency setup notes

Vitest uses the patched 4.1 release line, compatible with the existing Vite 6 and Node 20 setup. Some npm 10 versions crash with `Cannot read properties of null (reading 'edgesOut')` when resolving its optional peer dependencies. Use `npm ci` with the supplied lockfile for normal setup. If updating dependencies triggers this resolver error, use temporary newer tools without changing global installations:

```sh
npm exec --yes --package=node@22 --package=npm@11 -- npm install
```

The Python 3.12 backend installs with its existing dependency ranges. The current Starlette test client emits a deprecation warning for HTTPX; the health smoke test still passes. HTTPX remains the declared client dependency.

See [the setup review](docs/setup-review.md) for validation results, remaining development dependency vulnerabilities, and remediation limits.
