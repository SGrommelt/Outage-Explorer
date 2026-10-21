# Outage Explorer

A minimal monorepo for an outage exploration application, with a liveness API, React starter page, manual EIA schema probe, local date-window extraction and incremental refresh commands, and DuckDB views over published Parquet snapshots. Analytical API endpoints and the SQL Explorer UI are not implemented.

## Layout

```text
apps/
  backend/
    src/outage_explorer/
      api/health.py       # GET /health
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
cp apps/backend/.env.example apps/backend/.env
python -m uvicorn outage_explorer.main:app --reload --app-dir apps/backend/src
```

The API runs at http://127.0.0.1:8000. `GET /health` returns `{"status":"ok"}`. Interactive API docs are at http://127.0.0.1:8000/docs. No environment settings are required or loaded by the backend yet.

FastAPI, DuckDB, HTTPX, Pandas, PyArrow, Pydantic, and SQLGlot are declared dependencies. SQLite is provided by Python's built-in `sqlite3` module. Uvicorn runs the development server; Pytest is a development extra. No persistent database is initialized; analytical connections use an in-memory DuckDB catalog over existing Parquet files.

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

## DuckDB analytical model

The smallest model is three independent fact views. There are no copied data tables, inferred dimension tables, automatic joins, or new dependencies. Open a connection from the repository root with the installed backend:

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

The pointer is resolved once per connection. Publication during or after opening cannot mix versions: the existing connection stays pinned to its validated run. Open a new connection to observe a newly published snapshot. This relies on the existing contract that published files are immutable and old runs are retained; manual edits/deletion remain unsupported. The helper owns a fresh in-memory catalog; close it after use. It provides local model access only; SQL Explorer execution policy and API lifecycle integration are future work.

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

The function returns exactly one result for each requested inclusive date: `outage_date`, `total_fleet_capacity_mw`, `offline_capacity_mw`, `fleet_offline_share_pct`, and `status`. MW inputs preserve source Decimal precision. The percentage uses Decimal arithmetic with 60-digit working precision and explicit half-even rounding to six fractional percentage-point digits; DuckDB's floating-point division is not used. Thus 100 MW offline out of 300 MW returns `33.333333` percent. Rounded percentages may display 0 or 100 for values very near those endpoints. EIA's separately reported `percentOutage` is not substituted for the required formula or forced to match it.

A share is returned only for finite inputs with `capacity > 0` and `0 <= outage <= capacity` (`status='ok'`). This is the metric's interpretation policy, not a new extraction rule. Zero capacity and zero outage retain zero MW but yield a null share (`zero_capacity`), never 0% for 0/0. `missing_values`, `unsupported_values` (negative/nonfinite), and `inconsistent_values` (outage greater than capacity, including positive outage at zero capacity) also yield null shares; unsupported numeric totals are withheld. Duplicate days yield one `duplicate_key` result with both totals and share withheld rather than arbitrarily selecting or summing records. Missing days yield one `missing_date` result with null totals/share; this means absent from the local snapshot, not a proven EIA omission.

The existing `offline_capacity(db, start, end, level="national")` remains available for national, facility, or generator MW observations and the conditional `available_capacity_mw = capacity_mw - outage_mw` residual. Available capacity is not measured generation or guaranteed dispatchable output. Neither helper combines source grains, infers fully offline-reactor counts, sums MW across dates as energy, or reopens the snapshot pointer. Decimal/date serialization for an eventual API must be deliberate.

Strict snapshot opening normally rejects null/nonfinite values and duplicate keys. Synthetic Parquet tests cover the formula, varying dates, level isolation, zero/full/partial outages, large decimals and rounding; isolated in-memory faults exercise defensive statuses and missing/duplicate days. Run `apps/backend/.venv/bin/python -m pytest apps/backend/tests/test_capacity.py`. No source fields, dependencies, or extraction policies were added. The known facility completeness discrepancy still blocks the affected full three-route snapshot, even when querying only the national metric.

## Dependency setup notes

Vitest uses the patched 4.1 release line, compatible with the existing Vite 6 and Node 20 setup. Some npm 10 versions crash with `Cannot read properties of null (reading 'edgesOut')` when resolving its optional peer dependencies. Use `npm ci` with the supplied lockfile for normal setup. If updating dependencies triggers this resolver error, use temporary newer tools without changing global installations:

```sh
npm exec --yes --package=node@22 --package=npm@11 -- npm install
```

The Python 3.12 backend installs with its existing dependency ranges. The current Starlette test client emits a deprecation warning for HTTPX; the health smoke test still passes. HTTPX remains the declared client dependency.

See [the setup review](docs/setup-review.md) for validation results, remaining development dependency vulnerabilities, and remediation limits.
