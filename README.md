# Outage Explorer

A minimal monorepo for an outage exploration application, with a liveness API, React login and role-aware application shell, manual EIA schema probe, local date-window extraction and incremental refresh commands, and DuckDB views over published Parquet snapshots. Authenticated dataset catalog and preview APIs and restricted read-only SQL are available; Admin refresh jobs are available; the frontend includes authorized dataset previews, a restricted SQL Explorer, Admin refresh controls, and the national daily offline-share metric.

## Layout

```text
apps/
  backend/
    src/outage_explorer/
      api/health.py       # GET /health
      api/auth.py         # Login and current user
      api/datasets.py     # Authorized catalog and paginated previews
      api/query.py        # Validated single-dataset read-only SQL
      api/refresh.py      # Admin-only asynchronous refresh/status
      api/metrics.py      # Authenticated national fleet offline share
      refresh_jobs.py     # In-process refresh worker and safe job results
      main.py             # FastAPI app
      explore_eia.py      # Bounded schema probe
      extract_eia.py      # Validated Parquet snapshot extraction
      refresh_eia.py      # Incremental refresh and ingestion audits
      analytics.py        # DuckDB views over one published snapshot
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
test -e apps/backend/.env || cp apps/backend/.env.example apps/backend/.env
python -m uvicorn outage_explorer.main:app --reload --app-dir apps/backend/src
```

The API runs at http://127.0.0.1:8000. `GET /health` returns `{"status":"ok"}`. Interactive API docs are at http://127.0.0.1:8000/docs. Liveness remains public; authentication requires the local configuration below.

FastAPI, DuckDB, HTTPX, Pandas, PyArrow, Pydantic, and SQLGlot are declared dependencies. SQLite is provided by Python's built-in `sqlite3` module. Uvicorn runs the development server; Pytest is a development extra. No persistent database is initialized; analytical connections use an in-memory DuckDB catalog over existing Parquet files.

## Isolated real-data demo (completeness unverified)

The production facility count blocker remains unresolved. Use this **separate, read-only evidence demo** to demonstrate the application against the pinned real EIA archive for **September 22–October 21, 2026**. It contains 30 national, 1,650 facility, and 2,850 generator rows. It is **not independently certified complete, not a production publication, and not current live EIA data**. No EIA requests or EIA API key are needed.

After installing backend/frontend dependencies using the instructions above/below, configure the existing local `viewer`, `analyst`, and `admin` accounts using `seed_auth` below if not already configured. Do not replace existing credentials or put passwords in frontend settings. From the repository root, stop any production API using port 8000, then run:

```sh
# Validate the pinned archive and prepare a separate immutable demo dataset.
apps/backend/.venv/bin/python -m outage_explorer.demo_data
# Start the explicitly separate demo application (one worker).
apps/backend/.venv/bin/python -m uvicorn outage_explorer.demo:app \
  --host 127.0.0.1 --port 8000
```

In another terminal, from the repository root:

```sh
VITE_DEMO_MODE=true VITE_API_BASE_URL=/api npm run dev
```

Open Vite's printed URL (normally http://localhost:5173), sign in with your configured development passwords, select a dataset, or run `SELECT * FROM generator_outages ORDER BY outage_date LIMIT 10` as Analyst/Admin. Fleet offline share loads the latest archived period automatically. For an example observation, choose October 20, 2026: **100056.700000 MW** fleet capacity, **19859.767000 MW** offline, **19.848513%** offline share. Viewer can see only the national catalog/preview and national metric; SQL and refresh remain forbidden to Viewer. Use dates within the archived range to see real rows.

`VITE_DEMO_MODE` adds a visible warning on login and every application section; it is only a label and **does not select a backend or grant permissions**. The existing Vite `/api` proxy selects the server on port 8000. The demo backend independently labels every response with `X-Outage-Explorer-Data-Mode: demo-completeness-unverified`, a safe notice header, demo-prefixed catalog descriptions, and a demo OpenAPI title. Always use the explicitly named backend entry point and frontend flag together. No CORS change is needed with the proxy.

Demo Admin refresh accepts a normal asynchronous job and returns an ID; polling reaches **failed**, `published=false`, with an explicit read-only-demo message. This deliberately demonstrates admission/status/failure handling without simulating a publication, contacting EIA, or writing ingestion audits/watermarks. Job IDs remain in memory only. The unchanged production refresh job success/concurrency/recovery paths are tested offline with mocked ingestion, not claimed as a successful live refresh.

Artifacts live only under Git-ignored `data/eia-demo/datasets/<source-sha256>/`: three typed Parquet files and **`demo.json`**, which records the original reported totals, actual/unique counts, validation basis, and unresolved facility count caveat. There is **no `current.json`, production `manifest.json`, watermark, or ingestion audit**. Preparation is idempotent for unchanged valid artifacts and refuses to overwrite corrupt ones; move a damaged demo directory aside before preparing again. Startup fails if preparation is missing or corrupt and never falls back to production. Production readers and `--base-snapshot` adoption reject the demo root. Neither a query parameter nor a production environment switch enables demo data.

Validation checks the pinned archive hash, exact schemas/values/keys, all 30 route dates, terminal traversal, national/generator reported counts, matching facility/generator keys, and exact capacity/outage sums at all grains. These are internal consistency guarantees. Facility pages still report **2,850** against **1,650** validated rows and end empty at offset **1,650**; that discrepancy is stored, not corrected or certified. Related datasets can share omissions. The [investigation](docs/EIA_PAGINATION_INVESTIGATION.md) explains why this evidence does not change production completeness policy.

**Rollback/production:** stop the demo server and restart `outage_explorer.main:app` using the normal backend command, then restart Vite with `VITE_DEMO_MODE=false`. Production uses only its unchanged published snapshot root and may return 503 until a valid publication exists. Demo files are never automatically adopted; keep or move them independently. No production dataset needs restoration.

**Verification scope:** `tests/test_demo.py` uses actual FastAPI routers, real archive-derived Parquet, real password hashing/token issuance, temporary persona configuration, and ASGI TestClient. It covers logins/identity, catalog visibility, deterministic server pagination, filters/empty/errors, permissions before data access, safe/unsafe SQL, real metric values, expired tokens/account switching, read-only refresh jobs, and data/provenance corruption. Frontend tests use jsdom/mock HTTP and include the demo warning. **No browser-driven end-to-end session or user-specific configured-password login was performed.** The documented proxy/startup configuration is checked, but browser rendering/network behavior still needs manual verification.

## Local authentication and authorization

Create the backend `.env` if it does not already exist, preserving any configured EIA key, then run from the repository root:

```sh
apps/backend/.venv/bin/python -m outage_explorer.seed_auth
```

This interactively seeds **development-only test accounts** named `viewer`, `analyst`, and `admin`. Choose and confirm a distinct password of at least 12 characters for each. No default passwords exist, and passwords are never printed or saved in plaintext. The command generates salted PBKDF2-HMAC-SHA256 hashes (600,000 iterations) and a random 256-bit signing key, preserves unrelated `.env` entries, and writes the file with owner-only permissions. It refuses to replace existing configured auth credentials. The real local `.env` is Git-ignored; only `.env.example` is tracked. No credentials are seeded into source code or a database.

Restart the API after setup or configuration changes. `AUTH_SIGNING_KEY` is a 64-character hexadecimal secret, `AUTH_USERS_JSON` contains exactly three hashed accounts (one per role), and `AUTH_TOKEN_SECONDS` defaults to 600 (allowed 60–900). Exported AUTH variables override `.env`; interpolation is disabled. Missing/invalid configuration disables authentication with a generic 503 response while `/health` remains usable.

- `POST /auth/login`: JSON `{"username": "viewer", "password": "<your chosen password>"}` returns `access_token`, `token_type="bearer"`, and `expires_in`. Use the interactive API docs locally rather than putting real passwords in shell history.
- `GET /auth/me`: send `Authorization: Bearer <access_token>`; returns only username and role. Login/token responses use `Cache-Control: no-store`.
- Missing, malformed, tampered, expired, future-issued, wrong-key, and unknown-user tokens are rejected with generic **401** responses and a Bearer challenge. Authenticated permission failures return **403**. Validation errors omit submitted inputs to avoid credential echoes.

Tokens use a fixed versioned HMAC-SHA256 format with signed subject, issue/expiry times, and a nonce. Tokens are signed, not encrypted; they contain no password/hash. Roles are resolved from current server configuration, never trusted from token claims. Settings are cached per process, so account/key/role changes take effect after restarting the API. Rotating the signing key invalidates existing tokens. There is no refresh-token or revocation store; issued tokens expire after their short lifetime.

| Capability | Viewer | Analyst | Admin |
| --- | --- | --- | --- |
| National analytical dataset | Yes | Yes | Yes |
| Facility/generator datasets | No | Yes | Yes |
| Ad hoc SQL capability | No | Yes | Yes |
| Refresh capability | No | No | Yes |

`auth.current_user`, `require_roles`, `require_dataset`, `require_sql`, and `require_refresh` are reusable FastAPI dependencies. `DATASET_PERMISSIONS`, `allowed_datasets`, and `authorize_datasets` centralize the canonical analytical-view policy, rejecting unknown names by default. Catalog routes authenticate before listing and filter with `allowed_datasets`; preview routes apply `authorize_datasets` before opening a snapshot. The SQL route requires `require_sql` before SQL parsing, authorizes its one allowed dataset reference, and rejects subqueries/CTEs. Viewer is denied ad hoc SQL entirely, preventing SQL aliases, joins, or direct requests from becoming a path to restricted data. The SQL endpoint below adds a strict structural compiler and rejects resource-access functions; the role/dataset policy helper alone is not a SQL sandbox. Refresh start/status routes depend on `require_refresh` before touching job state, data, or invoking extraction.

Login, identity, public liveness, and authorized catalog/preview endpoints exist. Restricted SQL and Admin refresh start/status endpoints are implemented. Existing local Python/CLI analytical and extraction helpers are trusted internal tools, not authenticated HTTP endpoints; never expose their DuckDB connections or raw file access directly. The tests install temporary guarded routes with work counters to verify 401/403 occurs before any data read, query, or refresh work. Frontend controls are not the authorization boundary.

This local development foundation has no external identity provider, registration, password reset, or persistent account database. It adds no dependencies. Use HTTPS and deployment-level request/rate limits before exposing login outside a trusted local environment; access tokens are bearer credentials. Run `apps/backend/.venv/bin/python -m pytest apps/backend/tests/test_auth.py` for offline authentication/policy tests.

## Dataset catalog and previews (US-02, US-03, US-05)

Both routes require `Authorization: Bearer <access_token>` from `/auth/login`. They use the server's published snapshot at `data/eia/incremental` and apply role/dataset policy **before** opening a snapshot or DuckDB connection. Viewer sees only `national_outages`; Analyst and Admin also see `facility_outages` and `generator_outages`. Restricted direct URLs return 403 even with altered filters. Clients cannot supply file paths, SQL, dataset aliases, or sort expressions.

- `GET /datasets` returns `{"datasets": [...]}`. Each item has `name`, `description`, and `columns`, where each column has `name` and its DuckDB `type` (for example `DATE`, `VARCHAR`, or `DECIMAL(24,6)`). Only authorized schemas are returned.
- `GET /datasets/{dataset_name}/preview` returns `dataset`, `page`, `page_size`, `total`, and `rows`. `total` counts all rows matching the filters, before pagination. Empty results or pages beyond the last page return `rows: []`; an out-of-range page retains the filtered total.

Preview query parameters:

| Parameter | Behavior |
| --- | --- |
| `start_date`, `end_date` | Optional ISO `YYYY-MM-DD` dates, inclusive; either bound may be supplied. Reversed/invalid dates return 400. |
| `facility_id` | Optional exact, opaque string match, preserving leading zeros. Supported only for facility/generator data; national requests reject it with 400. Blank/padded values or values longer than 256 characters are rejected. |
| `page` | One-based integer, default 1; allowed 1–2,147,483,647. |
| `page_size` | Integer, default 50; strictly bounded to 1–200. |

Unknown or repeated query parameters are rejected for preview requests. All filter values, limits, and offsets are bound parameters. Dataset queries and order clauses come from a fixed server-owned mapping. Ordering is ascending by `outage_date`, then `facility_id` and `generator_id` where applicable, using the validated provisional natural keys.

For example, with a locally acquired token in `ACCESS_TOKEN`:

```sh
curl -H "Authorization: Bearer $ACCESS_TOKEN" http://127.0.0.1:8000/datasets
curl -H "Authorization: Bearer $ACCESS_TOKEN"   'http://127.0.0.1:8000/datasets/national_outages/preview?start_date=2026-10-18&end_date=2026-10-20&page=1&page_size=50'
# Analyst/Admin only:
curl -H "Authorization: Bearer $ACCESS_TOKEN"   'http://127.0.0.1:8000/datasets/generator_outages/preview?facility_id=46&page_size=25'
```

JSON dates are ISO strings, decimals are exact decimal strings (not binary-float JSON numbers), nulls remain JSON `null`, and pagination/count values are integers. Example preview response:

```json
{"dataset":"national_outages","page":1,"page_size":50,"total":1,"rows":[{"outage_date":"2026-10-20","capacity_mw":"100.000000","outage_mw":"20.000000","percent_outage":"20.000000"}]}
```

Authentication failures return 401; authenticated forbidden datasets return 403; unknown canonical dataset names return 404; invalid preview filters/pagination return 400. Missing, corrupt, or incompatible published data returns a generic 503, with no internal paths, SQL errors, or private snapshot metadata. Successful responses use `Cache-Control: no-store` and `Vary: Authorization` to prevent sharing cached results between personas.

Each request opens one validated, pinned connection and closes it afterward. Preview count and rows therefore use the same immutable snapshot even if publication changes mid-request. Separate page requests may observe newer publications; pagination is not pinned across requests. Existing full-snapshot validation scans all route files internally, but only authorized dataset columns and rows can leave the API; restricted validation failures are generic. This retains the current opening-time validation cost. A complete three-route publication is still required, so the known facility completeness discrepancy continues to block the affected real window. The preview endpoint does not accept SQL; use the restricted query endpoint below. Offline FastAPI coverage is in `apps/backend/tests/test_datasets.py`.

## Read-only SQL (US-04, US-05)

`POST /query` requires an Analyst or Admin bearer token. Viewer is denied with **403 before SQL validation or snapshot access**, including queries against national data. Missing/invalid authentication returns 401. The JSON body is `{"sql": "SELECT outage_date, outage_mw FROM national_outages ORDER BY outage_date DESC LIMIT 10"}`; extra fields and SQL longer than 10,000 characters are rejected.

The supported subset is intentionally small:

- Exactly one `SELECT` from one unqualified canonical dataset: `national_outages`, `facility_outages`, or `generator_outages`. Each reference is checked with the same central dataset policy before opening the snapshot.
- Projection is `*` alone or up to 32 plain, unqualified columns. Quoted column names are accepted; aliases and expressions are not. Result rows are arrays aligned with `columns`, preserving duplicate selected column names.
- Optional `WHERE` supports column/literal comparisons (`=`, `<>`, `<`, `<=`, `>`, `>=`), `AND`, `OR`, `NOT`, parentheses, `BETWEEN`, `IN` with up to 100 literals, `IS [NOT] NULL`, and `LIKE`. Literals include strings, booleans, nulls, decimal numbers with at most 18 integer digits and six fractional digits, and date literals such as `DATE '2026-01-01'`.
- Optional `ORDER BY` accepts plain columns with ASC/DESC and NULLS FIRST/LAST. Without it, the server orders by the dataset's natural key.
- Optional `LIMIT` is a nonnegative integer. No OFFSET, joins, subqueries, CTEs, unions, aggregates, functions, arithmetic, casts other than date literals, DISTINCT, qualified table names, table aliases, star modifiers, or other clauses are accepted. One trailing semicolon and ordinary comments are allowed; extra statements are not.

SQLGlot (already installed) parses the DuckDB dialect. The server checks the AST and its allowed arguments, rejects unsupported nodes/clauses, and builds fresh SQL using fixed dataset queries and canonical columns. **Original SQL text is never sent to DuckDB.** Literal values are bound parameters. All scalar/table functions and external scans are rejected, including DuckDB file readers, dynamic query functions, metadata functions, sequence mutations, and settings access. Writes, DDL, COPY, ATTACH, PRAGMA, extension loading, and multiple statements fail before opening a connection. AST size is capped at 512 nodes.

The server returns at most **200 rows**, regardless of a larger/absent user LIMIT. It fetches at most 201 rows to detect truncation, returning `columns` (objects with `name` and DuckDB `type`), `rows` (arrays), `row_count`, `row_limit=200`, and `truncated`. The flag means the **server cap** omitted rows from the requested result: a user LIMIT of 10 returns at most 10 with `truncated=false`, while a larger/unlimited result exceeding 200 returns `truncated=true`. Dates are ISO strings, decimals are exact decimal strings, and nulls stay null. Returned percentages/decimals are never converted to binary floats.

```sh
curl -X POST http://127.0.0.1:8000/query \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H 'Content-Type: application/json' \
  --data '{"sql":"SELECT outage_date, facility_id, outage_mw FROM facility_outages WHERE outage_mw > 0 ORDER BY outage_date DESC LIMIT 25"}'
```

Successful responses are `no-store` and vary by Authorization. Malformed/unsupported SQL, invalid request bodies, and binding/conversion errors return a generic 400 without SQL text, engine errors, or paths. Unknown/forbidden datasets return generic 403. Missing/invalid snapshots or storage failures return generic 503. Each request uses one validated pinned connection, closes it afterward, and exposes no snapshot metadata.

The strict subset is the SQL security boundary; this is not a general-purpose DuckDB shell. Syntax/row limits do not impose a hard execution-time or scan-memory budget: snapshot validation and allowed sorts/filters can still scan local data. Existing immutable-file/retention and full-snapshot validation assumptions remain. Only trusted published analytical views are exposed; expanding the subset or enabling any function requires explicit security review and tests. No caching, database writes, network/EIA requests, or new dependencies were added. Offline regressions are in `apps/backend/tests/test_query.py`.

## Admin refresh jobs (US-06)

Use a **single API worker** for this local challenge. Both `POST /refresh` and `GET /refresh/{job_id}` require an Admin bearer token. Viewer and Analyst receive 403, and unauthenticated/invalid-token requests receive 401, before job admission/lookup, credential loading, or refresh work. The background worker reuses `refresh_eia.refresh`; it does not introduce another extraction or publication path.

```sh
# Bootstrap only when no cumulative snapshot has been published:
curl -X POST http://127.0.0.1:8000/refresh \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H 'Content-Type: application/json' \
  --data '{"start_date":"2026-10-18","end_date":"2026-10-20"}'
# Subsequent refresh: start is derived from the current watermark.
curl -X POST http://127.0.0.1:8000/refresh \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H 'Content-Type: application/json' \
  --data '{"end_date":"2026-10-21","overlap_days":2}'
# Poll the Location returned by POST (Admin token required):
curl -H "Authorization: Bearer $ACCESS_TOKEN" http://127.0.0.1:8000/refresh/JOB_ID
```

POST accepts required `end_date`, optional bootstrap-only `start_date`, `overlap_days` (default 7, allowed 0–3660), and explicit `conflict_policy` (`reject` by default, or `replace` for reviewed corrections). It accepts no paths, credentials, client callbacks, or extraction bypass options. Invalid body/date order/options return 400. Mode/window rules requiring existing data (such as supplying a start for an existing snapshot or omitting it on first run) are checked asynchronously by the ingestion engine and recorded as job failures.

A successful admission returns **202**, a random `job_id`, `status="pending"`, and a `Location: /refresh/{job_id}` polling URL. This acknowledges admission, not data publication. States progress `pending → running → succeeded/failed`. Each response contains the request, UTC creation/start/finish timestamps, `fetched_rows` progress counts, final `row_counts` when successful, `published`, `outcome`, and nullable safe `error`/`warning` summaries. Counts use public dataset names and never combine levels. `fetched_rows` includes validated source pages only; it is not a published row count. Final counts describe merged snapshots (or the validated existing data for a no-op).

- `succeeded`, `outcome="published"`, `published=true`: the engine completed publication, or exception recovery validated the current snapshot as this run's publication. A post-publication error adds a generic warning; it cannot relabel an actually confirmed publication as an uncommitted failure.
- `succeeded`, `outcome="no_op"`, `published=false`: the engine validated a zero-overlap window with nothing new to fetch. Watermarks remain unchanged.
- `failed`: the worker did not confirm completion. `published=false` is used when the engine explicitly reports no publication or credentials are missing; `published=null` means the API cannot prove the publication outcome. If publication was reported but cannot be verified against the current pointer, a warning tells the Admin to inspect the local audit. A newer external CLI publication can make this determination ambiguous; the API never guesses success from an orphan manifest.

Another POST while a job is pending/running returns **409**, with no second admitted job or queue. Once the job finishes, a new start may be admitted. The pipeline's existing nonblocking filesystem writer lock additionally prevents overlapping extraction/publication with CLI writers or another process using the same output root. A competing external writer can cause the accepted API job to fail safely; it cannot corrupt or partially replace data. Existing bootstrap, overlap, conflict, completeness, schema, audit, retry, watermark, and atomic three-route publication policies remain unchanged. The facility 55-versus-95 discrepancy still fails; no bypass was added.

EIA credentials are loaded securely from the environment or `apps/backend/.env`, only inside the authorized worker, using the existing loader and fresh-connection HTTPX configuration. Missing credentials produce a failed job with `error="EIA credentials are not configured."`; the ingestion engine is still called with an empty key to persist its ordinary failed-attempt audit before networking. Unavailable storage can prevent audit creation. Responses never contain keys, request URLs, raw exceptions, filesystem paths, private run IDs, or snapshot versions. Unknown job IDs return 404; shutdown/unavailable admission returns generic 503. Polling a failed job returns HTTP 200 with its failed state. All job responses are `no-store` and vary by Authorization.

Catalog, preview, and SQL readers remain available while refresh runs: the dedicated background thread stages new data separately, and readers keep using their pinned immutable snapshot. Only the existing atomic pointer replacement changes what newly opened connections see; old files are retained.

**Job state is not durable across server restarts.** One in-process worker thread and a small in-memory history serve this local API; completed entries remain in memory until restart. There is no broker, distributed queue, cancellation API, automatic retry of whole jobs, or reconstruction of API job IDs from ingestion audits. Polling an old ID after restart returns 404. Durable pipeline audits and published snapshots remain on disk; the next engine invocation performs its existing audit recovery. Graceful API shutdown waits for admitted work and can delay reload; a forced stop may leave a running audit/unreferenced staging files, which never constitute a publication. Use one API process/worker: multiple workers have independent job dictionaries and polling can reach a worker that does not know the ID, even though the filesystem writer lock still prevents concurrent ingestion. Offline tests mock pipeline behavior and HTTP clients; no live refresh was run during implementation.

## Frontend

In a second terminal, from the repository root:

```sh
npm ci
cp apps/frontend/.env.example apps/frontend/.env
npm run dev
```

Open the URL printed by Vite (normally http://localhost:5173). The development proxy forwards `/api/*` to the local backend, so `/api/health` reaches `/health`. `VITE_API_BASE_URL` configures the client API base (default `/api`, without a trailing slash). Restart Vite after changing it. For production, configure an equivalent same-origin reverse proxy. An absolute cross-origin URL requires an explicit backend CORS policy; the backend currently has none.

Start the backend too using the instructions above, and seed the **development-only** `viewer`, `analyst`, and `admin` accounts using the existing `seed_auth` command. Sign in using the passwords you configured; there are no default or frontend-embedded passwords. Login calls `POST /auth/login`, then `GET /auth/me` before opening the workspace. Authentication works without a published data snapshot.

Tokens stay in memory only: reloads, tab closes, and logout require signing in again. The client clears its query cache on logout or expiry, schedules expiry from the login response, and centrally handles authenticated HTTP 401 responses. Login failures, backend/network failures, and expired sessions have separate user messages. A network error during identity verification leaves the workspace locked and permits another sign-in attempt. Logout clears the local token; it does not revoke an already issued backend token before its short expiry. Use HTTPS outside local development; never put secrets in browser-visible `VITE_*` settings.

The protected shell shows National datasets and Fleet offline share to Viewer, adds SQL Explorer for Analyst, and adds Refresh for Admin. The backend remains the authorization boundary; hidden navigation grants no security permissions.

In **Datasets**, select an authorized catalog entry to inspect its description, columns, and DuckDB types. Previews use server-side pagination (50 rows by default; selectable up to 200), with the filtered total and Previous/Next controls. Date bounds are inclusive; facility and generator datasets accept an exact facility ID. Changing datasets resets filters and pagination; changing any filter resets the page. National previews have no facility filter. Decimal strings, dates, identifiers, and nulls retain their API representation; numeric strings are not converted to JavaScript numbers. Filters update immediately without a debounce. Rows are hidden while the current request loads or fails. Each page is a separate backend request and can observe a newer published snapshot.

In **SQL Explorer**, use the example from an authorized catalog dataset or enter one SELECT against one dataset, selecting `*` or named columns with optional WHERE, ORDER BY, and LIMIT. The actual server subset supports basic comparisons, boolean combinations, LIKE, IN, BETWEEN, IS NULL, and DATE literals; joins, subqueries, aliases, functions, aggregation, and writes are rejected. Run the query to see typed result columns, returned row count, and whether the server's 200-row cap truncated results. Editing SQL clears previous results. There is no editor autocomplete or query history. Dataset, query, and cached session data are cleared on logout or expiry. Both interfaces surface safe validation, permission, network, and unavailable-snapshot messages; a valid complete local snapshot is required.

React, TypeScript, Vite, Tailwind CSS, TanStack Query, TanStack Table, Monaco Editor, and Recharts are configured or installed. QueryClientProvider is wired at the entry point. Tables, editor, and charts remain unused until features require them. shadcn/ui uses source-owned components rather than a runtime package; `components.json`, aliases, and the `cn` helper are ready for generating components into `src/components/ui`.

In **Refresh** (Admin only), enter an end date, an initial start date for the first publication, and the overlap length (default 7 days). Later runs derive their start from watermarks. The UI always uses the existing `reject` conflict policy. Start refresh submits once, displays the job ID, and polls status every two seconds while pending/running. Terminal status stops polling; timestamps, fetched/final row counts, publication/no-op outcome, and sanitized errors are displayed. A 409 means another job is active and no new job was admitted. A status-network failure pauses polling; use **Retry status** to resume. A failed start response may have occurred after admission: do not assume it cancelled the server job. Check the local audit if no job ID was received.

Leaving Refresh, logging out, or reloading stops client polling and clears the locally tracked job; it does **not** cancel server work. Copy the job ID to inspect it with the status API example above if needed. There is no persisted browser job history. Server restarts lose API job IDs; pipeline audits remain durable as described above. Expired authentication hides all job details and requires login again. The known EIA facility total mismatch can still make a real refresh fail; completeness and atomic publication remain strict.

In **Fleet offline share** (all three roles), the most recent national observation determines the default end date; the start is 29 days earlier, clipped to available history. No current-day or demo-date assumption is made. One metric request returns the latest bounded historical period and fills the date controls. You can choose another inclusive window of at most 366 days. A prominent latest-observation card shows the date, percentage, MW inputs, and formula. The responsive SVG trend uses a fixed 0–100% scale and breaks at missing/invalid days, with no interpolation. A true 0% is plotted. The accessible table retains exact server-returned values; only SVG coordinates use approximate JavaScript numbers. Missing/invalid percentages remain unavailable, including on the latest-day card. The existing demo warning remains visible; unavailable snapshots show a safe error with retry. Editing dates or changing accounts clears previous results. This metric has no facility or generator output.

MW columns in dataset previews, SQL results (recognized `_mw` names), reconciliation, and the metric use thousands separators and trim only insignificant trailing zeros. No meaningful fractional digits are rounded away, including micro-MW reconciliation gaps. Percentage columns follow the same rule; the metric remains rounded to six places by the backend, and the headline includes `%`. ISO dates (`YYYY-MM-DD`) are retained to avoid locale ambiguity. Column names/types and NULL markers remain visible.

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

Configure `EIA_API_KEY` in `apps/backend/.env` or export it in your shell. The exploration tool uses python-dotenv to load that specific file without overriding exported values or interpolating variables. Authentication reads its AUTH settings from local configuration; only an authorized Admin refresh job invokes EIA extraction. Catalog, preview, and SQL requests use local data only.

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

## DuckDB analytical model

See [the implemented data model](docs/DATA_MODEL.md) for exact physical/view types, the logical Mermaid ER diagram, invalid/duplicate/orphan handling, snapshot and audit relationships, and exactly seven explicit design decisions.

The production model is three independent fact views; the isolated demo entry point uses the same projections over separately validated, explicitly unverified evidence files. There are no copied data tables, inferred dimension tables, automatic joins, or new dependencies. Open a connection from the repository root with the installed backend:

```sh
apps/backend/.venv/bin/python - <<'PYTHON'
from pathlib import Path
from outage_explorer.analytics import open_analytics

with open_analytics(Path("data/eia/incremental")) as db:
    print(db.execute("SELECT * FROM national_outages ORDER BY outage_date LIMIT 10").fetchall())
PYTHON
```

The default root is `data/eia/incremental`; an explicitly supplied legacy date-window directory containing `current.json` also works. A complete published snapshot is required. The documented live facility discrepancy still prevents the affected real window from being opened; synthetic tests do not establish that real data are complete.

| View | Grain / provisional natural key | Additional columns |
| --- | --- | --- |
| `national_outages` | `outage_date` | None |
| `facility_outages` | `(outage_date, facility_id)` | `facility_id`, `facility_name` |
| `generator_outages` | `(outage_date, facility_id, generator_id)` | `facility_id`, `generator_id`, `facility_name` |

Every view exposes `outage_date` (`DATE`), `capacity_mw`, `outage_mw`, and `percent_outage` (each `DECIMAL(24,6)`). Identifiers are opaque `VARCHAR` values, preserving leading zeros and letters. `facility_name` remains a nullable display label. The projection renames source fields without casts or rounding. Validated source units are encoded by the measure names: megawatts and percent, where 2 means 2%, not 0.02. No unverified ratio or aggregate replaces EIA's reported percentage. These views do not declare SQL primary/foreign-key constraints; ingestion validates provisional key uniqueness.

Relationships are daily: facility observations relate to national observations by `outage_date`, and generator observations relate to facilities by `(outage_date, facility_id)`. A generator ID alone is not globally unique. Names are not join keys or stable dimensions. Cross-route entity coverage and reconciliation of totals are not proven. National measurements remain their own source observations, rather than sums reconstructed from facility or generator rows. Do not combine all three grains into one total, sum percentages, or sum parent measures after joining children. Aggregate each child dataset to the parent key before comparisons; no automatic join is built into the model.

Opening validates all three physical Parquet schemas, rows, keys, counts, daily coverage, and (for incremental snapshots) embedded publication metadata using the existing snapshot reader. `AnalyticsError` reports missing publications, incompatible schemas, or invalid snapshots; no partial connection is returned. Validation scans the existing datasets in memory, but writes no data, audits, or catalog files. Views subsequently scan explicit immutable Parquet paths with Hive partition inference disabled. They never glob staging files, orphan runs, or historical versions.

The pointer is resolved once per connection. Publication during or after opening cannot mix versions: the existing connection stays pinned to its validated run. Open a new connection to observe a newly published snapshot. This relies on the existing contract that published files are immutable and old runs are retained; manual edits/deletion remain unsupported. The helper owns a fresh in-memory catalog; close it after use. It provides local model access; the restricted `/query` endpoint applies the SQL execution policy, and the frontend SQL Explorer uses that endpoint.

Example SQL for a chosen snapshot (dates below are illustrative):

```sql
-- National outage trend: preserve reported percentage and exact daily measures.
SELECT outage_date, capacity_mw, outage_mw, percent_outage
FROM national_outages
WHERE outage_date BETWEEN DATE '2026-10-18' AND DATE '2026-10-20'
ORDER BY outage_date;

-- Facilities ranked by their largest reported daily outage in the window.
-- Group by identity, so display-name changes do not split a facility.
SELECT facility_id, MAX(outage_mw) AS peak_outage_mw
FROM facility_outages
WHERE outage_date BETWEEN DATE '2026-10-18' AND DATE '2026-10-20'
GROUP BY facility_id
ORDER BY peak_outage_mw DESC, facility_id
LIMIT 10;

-- Generator breakdown for a single facility and day.
SELECT facility_id, generator_id, facility_name,
       capacity_mw, outage_mw, percent_outage
FROM generator_outages
WHERE outage_date = DATE '2026-10-20' AND facility_id = '46'
ORDER BY generator_id;

-- Compare source levels only after reducing generators to the facility-day key.
-- Differences are diagnostic, not proof that either source level is incorrect.
WITH generators_by_facility AS (
    SELECT outage_date, facility_id, COUNT(*) AS generator_count,
           SUM(outage_mw) AS generator_outage_mw
    FROM generator_outages
    GROUP BY outage_date, facility_id
)
SELECT f.outage_date, f.facility_id, f.outage_mw AS facility_outage_mw,
       g.generator_count, g.generator_outage_mw
FROM facility_outages AS f
LEFT JOIN generators_by_facility AS g USING (outage_date, facility_id)
ORDER BY f.outage_date, f.facility_id;
```

The final query preserves one row per facility-day; a missing child group stays null. Summing reported MW observations across dates is not measured energy, and source percentages are not additive. The offline model tests cover both snapshot manifest versions, types/precision, keys and nullable labels, example query shapes, pointer changes during opening, unreferenced data, schema/publication corruption, and Hive-style/quoted paths. Run them with `apps/backend/.venv/bin/python -m pytest apps/backend/tests/test_analytics.py`.

## Reconciliation API and workspace (US-07)

**Reconciliation** navigation is visible to Analyst and Admin. Both endpoints below require those roles and authorization for all three analytical datasets **before opening any production/demo connection**. Viewer receives 403 even through direct URLs; missing/invalid/expired authentication receives 401. The endpoints reuse `reconcile_daily`, `reconcile_facilities`, and the existing diagnostic checks on a single read-only, pinned connection per request. They never alter files, audits, pointers, or watermarks.

```sh
# Default: latest 30 available days, clipped to the earliest available date.
curl -H "Authorization: Bearer $ACCESS_TOKEN" http://127.0.0.1:8000/reconciliation
# Explicit inclusive window; maximum 31 days.
curl -H "Authorization: Bearer $ACCESS_TOKEN" \
  'http://127.0.0.1:8000/reconciliation?start_date=2026-09-22&end_date=2026-10-21'
# Selected-date facility/generator comparisons, default 25 rows, maximum 100.
curl -H "Authorization: Bearer $ACCESS_TOKEN" \
  'http://127.0.0.1:8000/reconciliation/facilities?date=2026-10-21&page=1&page_size=25'
```

`GET /reconciliation` accepts optional `start_date` and `end_date`. Bounds are derived from the union of available dates across the three views, not hardcoded demo dates. End defaults to the latest observed date. Start defaults to 29 days before end, clipped to available start when applicable. An explicitly supplied start is preserved; supply an end if that would otherwise exceed 31 days or follow the available end. Explicit windows outside local coverage are allowed and show unavailable days, not invented zero measurements. An entirely empty readable source with no explicit bounds returns an empty result and null available bounds. Missing/incompatible published/demo data returns a generic 503 instead.

The daily response contains `start_date`, `end_date`, `available_start`, `available_end`, `source_completeness`, `summary`, and `rows`. `summary` includes `days_requested`, `days_compared` (matching plus difference days), `days_matching`, `days_with_differences`, and `days_unavailable` (missing-level or invalid-data days). Each requested calendar date gets one row, including dates absent at every grain. Each row includes:

- National reported, independently summed facility, and independently summed generator **capacity MW and outage MW**; all six pairwise signed gaps (facility−national, generator−national, generator−facility for each measure).
- `national_count`, `facility_count`, `generator_count`, `missing_facility_keys` (generator groups without parent observations), and `missing_generator_groups` (facilities without children), and `facility_groups_with_differences` (child/parent MW gaps that can cancel at the national level). Group comparison counts are null when invalid data makes those comparisons ambiguous.
- `status` (`equal`, `difference`, `missing_level`, `invalid_data`), `numeric_aggregates_match` (boolean or null when unavailable), and machine-readable `reasons` with user-readable descriptions. Numeric equality can coexist with a key difference or offsetting facility-level differences, such as an unmatched zero-valued group; that day is still a `difference`.

Decimal values serialize as exact strings; dates are ISO strings and missing/withheld values are null. Gap names `facility_minus_national_mw`, `generator_minus_national_mw`, and `generator_minus_facility_mw` refer to outage; the corresponding capacity keys are `facility_capacity_minus_national_mw`, `generator_capacity_minus_national_mw`, and `generator_capacity_minus_facility_mw`. No tolerance is applied: representable nonzero micro-MW gaps remain visible.

`GET /reconciliation/facilities` requires `date`, with `page` starting at 1 and `page_size` 1–100. It returns `date`, `page`, `page_size`, `total`, `source_completeness`, and bounded `rows`, ordered by facility identifier. Each row contains parent/child capacity and outage, signed generator−facility gaps, generator count, comparison status, and reasons. The helper computes one day's groups on the server, then slices the response; it never sends the whole period's thousands of facility rows by default. Pages beyond the last return empty rows. Separate requests may observe newer production publications; they are not pinned across page/date changes. The static demo remains on its one pinned evidence version.

| Reason code | Evidence-based meaning |
| --- | --- |
| `aggregates_match` | Numeric aggregates and observed facility keys agree; this is not independent fleet-coverage certification. |
| `missing_national_coverage`, `missing_facility_coverage`, `missing_generator_coverage` | That grain has no observation for the requested local date. This alone does not prove an EIA omission. |
| `missing_facility_key`, `missing_generator_group` | One observed side lacks the corresponding date/facility key on the other side; no lifecycle/deletion cause is inferred. |
| `facility_group_mismatch` | At least one facility differs from its generator sums even if daily differences cancel; inspect the selected date. |
| `capacity_mismatch`, `outage_mismatch` | Exact parent/independently summed child MW values differ. |
| `invalid_values`, `duplicate_keys`, `unsupported_values` | Ambiguous or unsupported observations cause the affected comparison's measures/gaps to be withheld, not silently summed/deduplicated. Range flags are diagnostic interpretation limits, not newly asserted EIA constraints. |
| `unknown_cause` | A difference is observed, but definitions, reporting timing, revisions, or coverage are unverified possible explanations—not established causes. |

Invalid/reversed/overlong dates, invalid pagination, duplicate or unknown parameters return 400. Errors never expose internal paths or raw engine details; successful responses use `no-store` and vary by Authorization. Strict snapshot validation normally rejects malformed/null/duplicate physical records before comparison, yielding 503. Tests deliberately modify temporary in-memory views to exercise defensive difference/unavailable states; no malformed production snapshot is accepted.

The UI shows period summary counts, all three daily grains/gaps/counts, evidence-based explanations, and a selected-date facility table with Previous/Next controls. Editing dates hides old results until applied; changing the detail date resets pagination. Network/error/empty states are explicit, expired sessions use the centralized 401 handler, and logout/account changes clear and abort old requests. MW formatting groups digits and removes insignificant trailing zeros **without binary-float conversion or rounding**, preserving all significant API digits. Tables scroll horizontally on small screens.

The backend always returns `source_completeness.independently_verified=false`. Demo mode prominently explains the unresolved facility reported-total mismatch using app-owned mode metadata, even without the frontend demo-label flag; production mode describes a validated publication without claiming an independently verified EIA inventory. All **30 real daily comparisons match**, and all **1,650 real facility-day comparisons match** in the archive; the UI initially receives just 25 of 55 facility groups on a selected date. No real differences or anomalies are fabricated. Production completeness validation, demo isolation, and publication/watermark behavior are unchanged. Tests cover real demo results plus synthetic missing dates/keys, micro-MW mismatches, duplicates/invalid values, zero-valued orphans, and authorization before any read.

## Reconciliation and anomaly diagnostics

`outage_explorer.diagnostics` queries an existing connection from `open_analytics`. It does not reopen the pointer, repair audits, alter Parquet, or change extraction rules. All results therefore stay on the same published snapshot as the analytical views. Functions return dictionaries/lists with native Python dates and exact `Decimal` measurements; an eventual API must serialize them deliberately without converting decimals to floats.

```python
from datetime import date
from outage_explorer.analytics import open_analytics
from outage_explorer.diagnostics import reconcile_daily, reconcile_facilities, find_anomalies

with open_analytics() as db:
    start, end = date(2026, 10, 18), date(2026, 10, 20)
    daily = reconcile_daily(db, start, end)
    facilities = reconcile_facilities(db, start, end)
    anomalies = find_anomalies(db, start, end)
    # To enable change flags, supply a justified Decimal threshold from your policy:
    # anomalies = find_anomalies(db, start, end,
    #                           change_threshold_mw=configured_threshold_mw)
```

All windows are inclusive Python `date` values. `reconcile_daily` compares national observations with independent facility and generator daily sums for both capacity and outage MW. `reconcile_facilities` first aggregates generators to `(outage_date, facility_id)` and full-outer joins to facility observations. This preserves unmatched groups and prevents multiplying a parent measure by its number of generators. Results include counts, exact signed differences, `category='source_comparison'`, and `status` of `equal`, `difference`, or `missing_level`. Delta names state their direction. A missing group/delta remains null, never an invented zero. Entirely absent dates are reported by the coverage check below. Percentages are neither summed nor compared to a derived ratio.

**A cross-level difference is not a confirmed source error.** The retained EIA metadata identifies the source and daily grains but does not establish an exact cross-route reconciliation formula, shared revision timing, complete entity coverage, or rounding rules. Source definition/aggregation/coverage/revision differences are possible explanations, not verified causes or blanket excuses. Exact equality is descriptive; any nonzero difference is retained without an invented tolerance. Missing parent/child groups are comparison findings, since entity lifecycle expectations are unknown. The known 55-versus-95 facility pagination issue remains an extraction failure, not something reconciliation makes acceptable.

`find_anomalies` separates these outputs:

| Result list | Category | Meaning / assumption |
| --- | --- | --- |
| `missing_dates` | `data_quality` | A route has no row for a day in the caller's explicitly expected window. At least one row per route-day is the existing ingestion coverage policy. |
| `invalid_records` | `data_quality` | Missing dates/required identifiers/measures, padded or blank identifiers, or nonfinite measures violate the stored-data contract. Null dates cannot be assigned to a window and are always reported. |
| `duplicate_keys` | `data_quality` | More than one observation has the same provisional natural key; this violates ingestion's uniqueness policy. |
| `range_flags` | `review` | Negative capacity/outage, outage greater than capacity, or percent outside 0–100 conflict with conventional physical interpretations. These ranges are NOT verified universal EIA constraints; flags do not reject or correct records. |
| `large_changes` | `review` | Absolute outage-MW change exceeds an explicitly supplied threshold. No default cutoff or statistical claim of unusualness is applied. |

The expected window is a caller assertion: normally choose dates within the published snapshot's declared coverage. Missing dates outside its coverage do not establish an EIA omission. There is no per-entity missing-date check, because plant/generator commissioning, retirement, and reporting lifecycles are not established. Strict snapshot opening already rejects missing route-days, invalid required data, and duplicate keys; these defensive diagnostics normally return empty quality lists for valid publications. Tests exercise those branches using isolated synthetic in-memory tables rather than relaxing snapshot validation or publishing malformed Parquet.

Change thresholds must be positive, finite `Decimal` values representable in `DECIMAL(24,6)`; floats and implicit rounding are rejected. Equality with the threshold does not flag. Changes partition by source level and full entity identity (including facility for generators), compare only consecutive calendar dates, and exclude invalid or duplicate observations. The previous observation can precede the requested start, avoiding a window-boundary blind spot. Gaps and ambiguous keys are not bridged, and changes are not normalized into percentages or rates. A large change is an inspection candidate, often a real outage/return-to-service event, not evidence of bad data.

The functions use a few local SQL scans and materialize their result lists for the caller. They add no persistent tables, dependencies, alerts, API endpoints, statistical models, or automatic corrections. Use bounded date windows for interactive work. The existing analytical opening-time full-data validation cost and immutable-file/retention assumptions remain unchanged.

Offline coverage includes reconciled totals, micro-MW differences, capacity differences, unmatched groups, missing boundary dates, empty routes, null/nonfinite values, duplicate keys, range flags, threshold boundaries, entity isolation, prior-window observations, reporting gaps, and snapshot pinning. Run `apps/backend/.venv/bin/python -m pytest apps/backend/tests/test_diagnostics.py`.

## Real-data reconciliation evidence (US-07)

The isolated `outage_explorer.analysis_eia` workflow covers **2026-09-22 through 2026-10-21 inclusive: 30 consecutive real days**. These are **non-production, completeness-unverified source observations**, not a published snapshot. No production pointers, ingestion audits, watermarks, RBAC, endpoints, or frontend behavior were changed. In particular, this analysis is not permission to publish facility data despite its unresolved count mismatch.

The compact [source evidence](docs/evidence/eia-real-30-days-source.json.gz) is approximately 61 KB compressed and includes exact response bodies, credential-free query parameters, per-response SHA-256 checksums, metadata, API version `2.1.14`, failure history, and collection/resumption timestamps. The [small result summary](docs/evidence/eia-real-30-days-summary.json) records the archive hash, all 30 daily comparisons, route counts, representative facility comparisons, and the explicit anomaly shortfall. Full facility-level output is reproducible locally rather than duplicated in Git. Checksums detect accidental changes; they are not an EIA signature or independent proof of provenance.

Offline reproduction from the repository root requires installed backend dependencies, **no API key and no network**:

```sh
apps/backend/.venv/bin/python -m outage_explorer.analysis_eia analyse \
  --input docs/evidence/eia-real-30-days-source.json.gz \
  --output data/eia-analysis/reproduced/analysis.json
apps/backend/.venv/bin/python -m pytest apps/backend/tests/test_analysis_eia.py
```

For a deliberately authorized new collection (not needed for replay), use a fresh evidence filename:

```sh
apps/backend/.venv/bin/python -m outage_explorer.analysis_eia fetch \
  --output data/eia-analysis/new-run/source.json.gz
# Optional --end YYYY-MM-DD fixes the end; otherwise use the minimum of the
# three advertised latest dates. The start is always 29 days earlier.
# Resume only missing pages after a failure, preserving the original archive:
apps/backend/.venv/bin/python -m outage_explorer.analysis_eia fetch \
  --resume-from data/eia-analysis/new-run/source.json.gz \
  --output data/eia-analysis/new-run/resumed-source.json.gz
```

Writes are restricted to a directory named `eia-analysis`; source destinations must be fresh. Production opening/publication functions are never called. Collection uses sequential 1,000-row pages, ascending period plus route identifiers, at most eight pages per route, two attempts per request, existing transient/429/5xx retry/backoff logic, a quarter-second request interval, and a **36-attempt total budget shared across resumptions**. It saves gzip checkpoints after successful responses and failures, keeps original totals, requests an explicit terminal page, and retains safe failures. Exported EIA credentials take precedence over `apps/backend/.env`; no key or request echo is retained. Resume validates saved evidence and uses its original window, skipping already terminated routes. Offset pagination has no source snapshot isolation; later revisions or shifting data can still cause inconsistent evidence.

### Provenance, coverage, and request outcome

Collection started **2026-10-21T20:43:21.751480Z**; the targeted resume started **2026-10-21T20:46:11.439075Z**. A sandbox preflight exhausted six connection attempts without HTTP responses. The permitted collection and resume used **14 network attempts: 12 HTTP 200 responses and two failed network attempts** at the generator continuation after 2,000 rows. One automatic retry was attempted before the targeted resume fetched only the remaining 850 rows and terminal page. The original partial archives remain locally under Git-ignored `data/eia-analysis/real-30-days/`; the retained source archive includes the failure history. Network failures are not empty data pages or source-data anomalies.

| Route | Validated / unique records | Days present | Source-reported total | Returned pages (rows) |
| --- | ---: | ---: | ---: | --- |
| National | 30 / 30 | 30 | 30 | 30, 0 |
| Facility | 1,650 / 1,650 | 30 | 2,850 | 1,000, 650, 0 |
| Generator | 2,850 / 2,850 | 30 | 2,850 | 1,000, 1,000, 850, 0 |

All received rows pass the existing schema, required-value, units, date-window, exact decimal precision, and natural-key validation. Every date has one national row, 55 facility rows, and 95 generator rows. The facility route remains unresolved: an empty page occurs at offset **1,650**, while its unchanged reported total is **2,850**. This is one API metadata/pagination issue, not 30 or 1,200 independent source-data anomalies. The count-before-aggregation interpretation remains a hypothesis. Row-count agreement on the other routes and cross-route equality do not independently establish complete source coverage.

### Comparison method and concrete results

Offline replay uses the existing `schema_for`/`validate_record`, the fixed analytical `VIEWS` projections in an isolated in-memory DuckDB connection, and `reconcile_daily`, `reconcile_facilities`, and `find_anomalies`. Generators are aggregated to facility/day before comparing to facility rows. Each source grain is aggregated independently by day, never combined into a mixed-grain total. Percentages are not summed. All capacity/outage arithmetic and gaps use Decimal values. The daily output adds generator-minus-facility capacity/outage gaps alongside the existing national-relative gaps, so all three pairs are explicit.

**All 30 daily comparisons and all 1,650 facility-day comparisons are exactly equal for both capacity and outage MW.** Every pairwise gap is `0.000000` MW; there are no unmatched facility/generator groups. Representative actual observations follow; each parenthesized pair is **(capacity MW, outage MW)**:

| Date | National reported | Facility sum | Generator sum | All pairwise MW gaps |
| --- | --- | --- | --- | --- |
| 2026-09-22 | (97620.500000, 7745.638000) | (97620.500000, 7745.638000) | (97620.500000, 7745.638000) | 0.000000 |
| 2026-10-06 | (100056.700000, 15268.765000) | (100056.700000, 15268.765000) | (100056.700000, 15268.765000) | 0.000000 |
| 2026-10-21 | (100056.700000, 19936.436000) | (100056.700000, 19936.436000) | (100056.700000, 19936.436000) | 0.000000 |

| Date | Facility ID | Generators | Facility capacity = generator sum MW | Facility outage = generator sum MW | Capacity / outage gaps MW |
| --- | --- | ---: | ---: | ---: | --- |
| 2026-09-22 | 46 | 3 | 3661.700000 | 1227.400000 | 0.000000 / 0.000000 |
| 2026-10-06 | 6008 | 3 | 4003.000000 | 1333.000000 | 0.000000 / 0.000000 |
| 2026-10-21 | 3166 | 2 | 2694.100000 | 1346.700000 | 0.000000 / 0.000000 |

These are **reconciled examples, not anomalies**. The observed equality is consistent with compatible aggregation, but shared source omissions, definitions, or revisions remain possible. No actual nonzero reconciliation gap was found to explain away. Fleet capacity changes from `97620.500000` MW on September 30 to `100056.700000` MW on October 1, a `2436.200000` MW increase consistently present at all three grains. The offline temporal investigation below accounts for the full increase using stable-ID capacity changes and rules out membership change within the received population; the underlying capacity-basis methodology remains unconfirmed. A change alone violates no verified invariant.

### Anomaly evidence and explicit shortfall

**Zero source-data anomalies were observed under the implemented checks; the requirement for three genuine anomalies is not met.** The replay finds zero missing route dates, invalid required records, duplicate natural keys, negative capacity/outage values, outage-above-capacity values, or percentages outside 0–100. No capacity/outage reconciliation differences or unmatched groups were found. This describes only these 4,530 received observations and the documented rules, not the entire EIA history or the unverified population behind the facility total.

The one verified API issue is the facility `total=2850` / 1,650 returned rows / empty offset-1650 page. Diagnostic handling retains the original responses, records the unresolved completeness flag, and labels every result non-production. Production ingestion still rejects it and never advances watermarks or publishes it. The temporary generator coverage gap from network failure was fixed by resume and is not counted as an EIA anomaly.

Ordinary source rounding is not treated as an anomaly. Automatic large-change checks were deliberately not enabled without a justified operational baseline or threshold; normal shutdown/restart transitions are not automatically data errors. The product's existing diagnostic range rules flag candidates as `review`, not proven source defects. Invalid records would withhold this workflow's reconciliation rather than silently skip or deduplicate them. Published-snapshot validators and the fleet-share calculation retain their existing rejection/null-status behavior.

The smallest follow-up is to agree an independently justified anomaly definition or an earlier bounded period with externally evidenced unusual observations, then run the same workflow—not invent three examples from this internally consistent window. EIA clarification of facility count/grain semantics is separately still needed for trustworthy production publication. The retained source and summary allow every stated value and zero-anomaly result to be reproduced offline.

## Offline temporal investigation and anomaly shortfall

The follow-up inspected the **same saved September 22–October 21, 2026 evidence** and made **zero live EIA API requests**. [The temporal summary](docs/evidence/eia-real-30-days-temporal-summary.json) records counts, exact capacity decomposition, representative generator values, source archive hash, and the explicit shortfall. The raw evidence archive was not changed. Reproduce the full candidate inventory without credentials/network:

```sh
apps/backend/.venv/bin/python -m outage_explorer.analysis_eia inspect \
  --input docs/evidence/eia-real-30-days-source.json.gz \
  --output data/eia-analysis/temporal-inspection.json
apps/backend/.venv/bin/python -m pytest apps/backend/tests/test_analysis_eia.py
```

The command reuses `analyse()` for source/checksum/schema/key validation and the existing reconciliation/diagnostics, then compares adjacent calendar days within each source grain. It never bridges a missing date. It inventories ID additions/removals, stable-ID capacity changes, label changes/missing labels, facility-versus-generator label discrepancies, undefined ratios, and percentage arithmetic candidates. Nullable/changed labels and membership changes are candidates for interpretation, not automatically defects or proof of lifecycle violations. SQL for these investigations is trusted offline logic, not an expansion of the restricted HTTP SQL subset.

### September 30–October 1 capacity decomposition

Within the received evidence, all **55 facility IDs and 95 `(facility, generator)` identities are present on all 30 days**. No IDs appear or disappear. All recorded capacity changes occur at October 1: **47 facilities and 78 generators** increase capacity, with 8 facilities and 17 generators unchanged. Both the facility and generator increases sum exactly to the national **2436.2 MW** step from **97620.5 MW** to **100056.7 MW**. Added and removed capacity contributions are **0 MW** at each grain. The full report's `capacity_changes` lists every changed key, and `capacity_decomposition` independently balances each grain's stable-key changes plus additions minus removals against its total change.

Actual generator examples (one coordinated capacity-basis candidate, **not three anomalies**):

| Facility | Generator | 2026-09-30 capacity MW | 2026-10-01 capacity MW | Change MW |
| --- | --- | ---: | ---: | ---: |
| 46 | 1 | 1227.4 | 1254.8 | +27.4 |
| 3166 | 2 | 1264.7 | 1346.7 | +82.0 |
| 7722 | 1 | 1123 | 1179 | +56 |

**Verified explanation:** the step is entirely reported capacity changes for existing identifiers in the returned population, not a membership addition/removal. No changed name or cross-grain disagreement accompanies it. One collected vintage cannot establish whether EIA revised earlier published values. **Unconfirmed explanation:** a coordinated capacity-rating refresh or seasonal rating basis could account for the synchronized increases. EIA's [general capacity FAQ](https://www.eia.gov/tools/faqs/faq.php?id=101) distinguishes summer/winter ratings and their test conditions, while its [nuclear data index](https://www.eia.gov/nuclear/data.php) labels the outage product as using annual capacity. Neither reviewed reference nor the retained route metadata specifies an October 1 switch for these routes. We therefore do not claim a proven seasonal mechanism, actual new installed capacity, or a data defect. No documented invariant requires reported capacity to remain constant across dates.

Product handling preserves each day's reported capacity and uses that day's national denominator for US-08. Date-valid, finite, representable changes do not themselves violate ingestion rules. The offline inspector exposes stable-ID changes for review without changing production diagnostics, acceptance, watermarks, or publication.

### Candidate search outcomes and anomaly classification

| Investigation | Observed outcome | Classification / handling |
| --- | --- | --- |
| Membership continuity | Zero facility/generator ID changes across 29 adjacent-day intervals per grain | No demonstrated lifecycle anomaly; not independent fleet-coverage proof |
| Capacity changes for stable IDs | 78 generators / 47 facilities change on October 1; national step fully explained numerically | One coordinated review candidate, no verified violated invariant |
| Source percentages versus outage/capacity | Zero values outside the nearest-two-decimal rounding interval across all 4,530 received observations | No arithmetic candidate under that explicit assumption |
| Names and other required attributes | Zero missing labels, zero temporal name changes, zero facility/generator label mismatches; required dates/IDs/measures/units already validated | No demonstrated attribute defect |
| Negative/impossible/ambiguous observations | Zero existing range, invalid-record, duplicate-key, or missing-route-date flags | No source-data defect under these checks |
| Facility pagination total | Window `total=2850`, 1,650 unique rows, empty page at offset 1,650, despite 30 dates present | **One verified API metadata/pagination anomaly**; production rejects the run; diagnostic analysis remains unverified |

Source percentage strings have zero to two fractional digits in this archive: national counts are 28 with two digits and 2 with one; facility counts are 305 with two, 52 with one, and 1,293 whole numbers; all 2,850 generator percentages are whole numbers. String formatting alone does not establish a source rounding contract. The inspector checks the **0.005 percentage-point half-unit interval**, using exact cross multiplication (`abs(percent * capacity - outage * 100) > 0.005 * capacity` for positive capacity) and 60-digit Decimal working precision. Equality at the boundary is not flagged. This avoids mistaking normal rounding for a discrepancy. It is an **explicit nearest-two-decimal comparison assumption motivated by the maximum displayed precision**, not a newly asserted EIA rounding/formula contract; any future candidate would require source-definition review before being called erroneous. Capacity <= 0 yields an undefined-ratio inventory entry, not division or zero substitution. No arbitrary "large daily change" threshold was introduced.

**Shortfall:** this investigation found **zero confirmed source-data anomalies and no new independent anomaly beyond the one known API pagination issue**. Three-anomaly delivery is still incomplete. It would be misleading to count the 78 component capacity changes as separate defects, count the pagination mismatch separately for each day, or present ordinary outage starts/ends as bad data. The raw percentages, exact reconciliation, stable identifiers, and attribute checks provide no defensible second or third source anomaly in this window.

Concrete percentage checks further illustrate why ordinary rounding is not counted:

| Date / scope | Capacity MW | Outage MW | Reported % | Calculated % (approx.) | Reported minus calculated, percentage points (approx.) |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-10-19 / national | 100056.7 | 19896.304 | 19.89 | 19.885029188 | +0.004970812 |
| 2026-09-27 / facility 6026 | 2264.4 | 226.1 | 9.98 | 9.984984985 | -0.004984985 |
| 2026-09-22 / facility 46, generator 1 | 1227.4 | 1227.4 | 100 | 100 | 0 |

These are maximum absolute percentage gaps at the respective grains, all within the stated interval; generator ratios agree exactly throughout this sample. Exact 60-digit diagnostic ratios/gaps are retained in the temporal summary. They are **not three anomalies**.

A small extension was considered, including a few dates around another seasonal/month boundary. Without an external rating-definition or lifecycle expectation, another coordinated step would still be only a candidate, not proof of an invariant violation; a blind nearby download is not currently justified. **No extension or additional source-data requests were made.** The smallest next evidence is EIA confirmation of capacity basis/effective-date rules (and independently the facility count semantics), or an independently documented specific entity/date inconsistency to target with a small bounded request. Additional dates alone cannot guarantee three genuine anomalies. The [data model and seven design decisions](docs/DATA_MODEL.md) are documented independently of this unresolved requirement.

## US-08: daily fleet capacity offline

**US-08:** “As an Analyst, I want the share of total fleet capacity offline per day, ready-made.”

`outage_explorer.capacity.daily_fleet_offline_share` is the primary metric. It uses only the national analytical view: retained EIA metadata identifies `us-nuclear-outages` as **U.S. Nuclear Outages**, specifies one observation per day with no entity facets, and labels both `capacity` and `outage` in **megawatts**. These are EIA's reported U.S. nuclear capacity/outage totals, not sums reconstructed from the other levels. See the verified [schema and recorded metadata](docs/EIA_SCHEMA.md). Exact fleet membership, capacity-rating methodology, and revision/reconciliation rules are not established by that metadata.

```text
fleet_offline_share_pct = national_outage_mw / national_capacity_mw * 100
```

```python
from datetime import date
from outage_explorer.analytics import open_analytics
from outage_explorer.capacity import daily_fleet_offline_share

with open_analytics() as db:
    rows = daily_fleet_offline_share(db, date(2026, 10, 18), date(2026, 10, 20))
```

Both date parameters are optional: when omitted, the endpoint derives the latest bounded 30-day period from national observations in the same pinned connection; an empty source returns `rows: []`. Explicit date ranges retain their existing calculation and validation semantics.

The authenticated API exposes the existing calculation through `GET /metrics/fleet-offline-share?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`:

```sh
curl -H "Authorization: Bearer $ACCESS_TOKEN" \
  'http://127.0.0.1:8000/metrics/fleet-offline-share?start_date=2026-10-18&end_date=2026-10-20'
```

The response is `{ "rows": [...] }`, with the fields below; dates and Decimal values are strings and unavailable values are JSON null. All three roles are authorized through the existing `national_outages` policy, checked before opening the pinned snapshot. Missing/invalid authentication returns 401, invalid/missing dates or windows longer than 366 inclusive days return 400, and unavailable/incompatible publications return a generic 503. No raw source paths, snapshot metadata, or lower-grain identifiers are returned. Production snapshot validation can reject malformed source records before the calculation runs; defensive calculation statuses do not bypass that validation.

The function returns exactly one result for each requested inclusive date: `outage_date`, `total_fleet_capacity_mw`, `offline_capacity_mw`, `fleet_offline_share_pct`, and `status`. MW inputs preserve source Decimal precision. The percentage uses Decimal arithmetic with 60-digit working precision and explicit half-even rounding to six fractional percentage-point digits; DuckDB's floating-point division is not used. Thus 100 MW offline out of 300 MW returns `33.333333` percent. Rounded percentages may display 0 or 100 for values very near those endpoints. EIA's separately reported `percentOutage` is not substituted for the required formula or forced to match it.

A share is returned only for finite inputs with `capacity > 0` and `0 <= outage <= capacity` (`status='ok'`). This is the metric's interpretation policy, not a new extraction rule. Zero capacity and zero outage retain zero MW but yield a null share (`zero_capacity`), never 0% for 0/0. `missing_values`, `unsupported_values` (negative/nonfinite), and `inconsistent_values` (outage greater than capacity, including positive outage at zero capacity) also yield null shares; unsupported numeric totals are withheld. Duplicate days yield one `duplicate_key` result with both totals and share withheld rather than arbitrarily selecting or summing records. Missing days yield one `missing_date` result with null totals/share; this means absent from the local snapshot, not a proven EIA omission.

The existing `offline_capacity(db, start, end, level="national")` remains available for national, facility, or generator MW observations and the conditional `available_capacity_mw = capacity_mw - outage_mw` residual. Available capacity is not measured generation or guaranteed dispatchable output. Neither helper combines source grains, infers fully offline-reactor counts, sums MW across dates as energy, or reopens the snapshot pointer. The metric API serializes Decimal values and dates explicitly as strings.

Strict snapshot opening normally rejects null/nonfinite values and duplicate keys. Synthetic Parquet tests cover the formula, varying dates, level isolation, zero/full/partial outages, large decimals and rounding; isolated in-memory faults exercise defensive statuses and missing/duplicate days. Run `apps/backend/.venv/bin/python -m pytest apps/backend/tests/test_capacity.py`. No source fields, dependencies, or extraction policies were added. The known facility completeness discrepancy still blocks the affected full three-route snapshot, even when querying only the national metric.

## Dependency setup notes

Vitest uses the patched 4.1 release line, compatible with the existing Vite 6 and Node 20 setup. Some npm 10 versions crash with `Cannot read properties of null (reading 'edgesOut')` when resolving its optional peer dependencies. Use `npm ci` with the supplied lockfile for normal setup. If updating dependencies triggers this resolver error, use temporary newer tools without changing global installations:

```sh
npm exec --yes --package=node@22 --package=npm@11 -- npm install
```

The Python 3.12 backend installs with its existing dependency ranges. The current Starlette test client emits a deprecation warning for HTTPX; the health smoke test still passes. HTTPX remains the declared client dependency.

See [the setup review](docs/setup-review.md) for validation results, remaining development dependency vulnerabilities, and remediation limits.
