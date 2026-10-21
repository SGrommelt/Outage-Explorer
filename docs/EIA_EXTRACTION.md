# Local EIA nuclear outage extraction

This command builds on the observed wire schema in [EIA_SCHEMA.md](EIA_SCHEMA.md). It uses HTTPX, PyArrow, and python-dotenv already declared by the backend. It does not introduce a database, scheduler, distributed infrastructure, or web API endpoints.

## Run from the repository root

Install the backend and configure `EIA_API_KEY` in `apps/backend/.env` or the environment, as documented in the README. Exported values take precedence. Keys are not command-line arguments and are never logged. The extractor reuses the exploration tool's environment loader and metadata/page validators; the exploration command remains unchanged.

```sh
apps/backend/.venv/bin/python -m outage_explorer.extract_eia \
  --start 2026-10-20 --end 2026-10-20 \
  --page-size 25 --attempts 3 --timeout 20
```

`--start` and `--end` are required inclusive daily ISO dates. Start with a small window. No full-history download has been run.

| Option | Default | Bounds / behavior |
| --- | --- | --- |
| `--page-size` | 500 | 1–5000 rows per request |
| `--max-pages` | 10000 | 1–100000 pages per route; exceeding the limit fails without publication |
| `--attempts` | 4 | 1–6 total attempts per request, including the first |
| `--backoff` | 1 second | Finite value 0–30; exponential delays, capped at 30 seconds |
| `--timeout` | 30 seconds | 1–60 seconds per HTTP operation |
| `--output-dir` | `data/eia` | Local output root; use a location on a filesystem supporting atomic rename |
| `--env-file` | `apps/backend/.env` | Explicit default based on module location, not shell working directory |

All three verified routes are requested. Sorting is explicitly ascending by `period`, followed by `facility` and `generator` where applicable. Every request fixes the same start/end window and measure projection. Offset advances by the actual rows returned. A short page is not assumed complete while the reported total says rows remain.

Only transient network errors/timeouts, HTTP 429, and HTTP 5xx are retried. Default exponential waits are 1, 2, and 4 seconds. Retry-After seconds or HTTP dates are respected within the 30-second delay cap. Longer requested waits are clamped; retries remain bounded and may exhaust under sustained throttling. Authentication errors, redirects, malformed responses, warnings, validation errors, and completeness failures are not retried automatically. Fresh HTTP connections retain the successful exploration workaround; TLS verification remains enabled.

Logs are JSON lines on stderr: start/window, retries and delays, page counts/totals, route completions/failures, elapsed seconds, and publication status. Logs omit request URLs, keys, raw records, and remote error bodies. HTTPX/HTTPCore request logging is disabled. The CLI returns 0 after publication, 1 on failure, and 130 on interruption. A storage error or interruption after the atomic publication point may leave a complete new snapshot already current; check the pointer and publication logs rather than assuming failure means no pointer change.

## Validation policy and schemas

The natural keys remain provisional across full history. They are enforced within each extracted window:

| Route | Candidate natural key |
| --- | --- |
| `us-nuclear-outages` | `period` |
| `facility-nuclear-outages` | `(period, facility)` |
| `generator-nuclear-outages` | `(period, facility, generator)` |

Each route has a fixed Arrow schema across runs. Source field names are retained:

- `period`: non-null `date32`, valid ISO calendar date inside the requested window.
- `facility` and `generator`, where applicable: non-null strings, preserved without integer coercion; empty/whitespace-padded identifiers fail.
- `facilityName`, on facility/generator: nullable string. Absence/null is allowed because the display name does not establish identity.
- `capacity`, `outage`, `percentOutage`: non-null `decimal128(24, 6)`, representing up to 18 integer digits and six fractional digits. This is an explicit storage choice, not a claimed EIA precision guarantee. Numeric strings, native integers, and native decimal JSON numbers are parsed without an intermediate binary-float conversion. Nonfinite values, nulls, blanks, invalid numbers, overflow, or values needing rounding are rejected. Zero remains zero; negative values are not rejected based on an unverified range assumption.
- `capacity-units` and `outage-units`: non-null strings, required to equal `megawatts`.
- `percentOutage-units`: non-null string, required to equal `percent`.

Metadata must still advertise daily data, the known identifier dimensions, all three requested measures, and expected units. Unexpected row fields fail for schema review. Response frequency/date format, array structure, totals, and page bounds are checked. Totals must remain constant and equal the number of downloaded rows at completion. Empty results, early empty pages, and duplicate candidate keys (even identical duplicates) fail; nothing is silently skipped or deduplicated. Every route must cover every date in the requested window. This is a conservative acceptance policy, not a guarantee that EIA always supplies such coverage.

Malformed records are reported by route/page offset and row index, with a safe reason. Raw values are not logged or saved in a rejection file. The entire snapshot is rejected rather than publishing only the good rows. Row validation and Parquet writing happen page by page; candidate-key and date sets remain in memory for uniqueness/coverage checks.

## Output and idempotency

Only a successful complete run creates/updates `current.json`:

```text
data/eia/
  2026-10-20_2026-10-20/
    current.json                   # {"run": "runs/<unique-run-id>"}
    runs/<unique-run-id>/
      us-nuclear-outages.parquet
      facility-nuclear-outages.parquet
      generator-nuclear-outages.parquet
      manifest.json
```

The manifest records the inclusive window, creation timestamp, schema version, API versions, candidate keys, record counts, page counts, and relative filenames. It contains no key or echoed request. Parquet uses Zstandard compression. Footer row counts and Arrow schemas are checked before publication.

All route files are first written under a unique `.staging-*` directory on the output filesystem. They are closed, checked, and fsynced together with the manifest. The staging directory is atomically renamed to an immutable run, then a flushed temporary pointer is atomically moved over `current.json`. Readers resolve `current.json` once and open the three files from that run. An exception before publication leaves the previous pointer and files unchanged. Route validation failures are accumulated so the other routes can still be diagnosed; storage failures and interruptions abort immediately.

A repeated successful run of the same window produces another complete immutable version and updates the pointer; it does not append rows. Thus the **current logical dataset** is idempotent for unchanged source data. Earlier runs are retained, not merged. Concurrent runs use unique staging/run/pointer files; the last successful publisher wins with a complete snapshot. Distinct or overlapping date windows are independent snapshots, not a combined history table. Globbing every Parquet file across windows/runs would duplicate data and is not a supported read pattern.

For example, after a successful run:

```python
import json
from pathlib import Path
import pyarrow.parquet as pq

window = Path("data/eia/2026-10-20_2026-10-20")
run = window / json.loads((window / "current.json").read_text())["run"]
national = pq.read_table(run / "us-nuclear-outages.parquet")
```

Normal exceptions/SIGINT clean staging files. SIGKILL, process crashes, or a failure between the run rename and pointer update can leave unpublished staging directories or orphan complete runs. Readers ignore these because they do not follow the current pointer. Old/orphan snapshot garbage collection is deliberately not implemented. No existing successful run is deleted. All default output is under the Git-ignored `data/` directory.

## Actual small-window validation, 2026-10-21

The live command above was run for **2026-10-20 only**, with page size 25, fresh connections, a 20-second timeout, and at most three attempts per request. The complete diagnostic run finished in approximately 4.8 seconds; no retries were needed.

| Route | Rows downloaded and individually validated | API-reported total | Result |
| --- | --- | --- | --- |
| National | 1 | 1 | Complete, staged successfully |
| Facility | 55 | 95 | Rejected: pages contained 25, 25, 5, then 0 rows at offset 55 |
| Generator | 95 | 95 | Complete, staged successfully |

No duplicate candidate keys, invalid dates, missing required fields, invalid measures, or unit mismatches were found in these 151 records. This does not establish full-history data quality. The facility count discrepancy reproduces the suspicious equal facility/generator totals noted during exploration. The API response is inconsistent with its own total; it is not safe to infer which signal is authoritative from this run alone.

**The live command correctly returned exit status 1 and published zero datasets.** Staged files were removed. No `current.json` exists for this failed window, and no prior published data was replaced. Tests independently verify preservation of existing snapshots under this exact overreported-total pattern. There is deliberately no flag to bypass completeness validation. The pipeline is implemented, but a complete real three-route snapshot remains blocked on resolving the upstream facility-count discrepancy.

The [concluded pagination investigation](EIA_PAGINATION_INVESTIGATION.md) independently reproduced 55 rows versus `total=95`, then an empty page at offset 55. Smaller pages and reversed sorting returned the same 55 facilities. Generator and national controls matched their totals. Counting generators before facility aggregation is a supported hypothesis, not a confirmed cause. Strict completeness validation and atomic publication remain unchanged; live investigation has stopped.

## Checks and limits

Run all backend tests offline from the repository root:

```sh
apps/backend/.venv/bin/python -m pytest apps/backend/tests
```

Tests cover all routes, pagination and total inconsistencies, numeric conversion/precision, identifiers/dates/required fields, duplicate keys, retries and retry exhaustion, Retry-After, malformed responses, repeat-run idempotency, HTTP/disk failures, interrupted extraction, atomic publication, and credential-safe structured logs.

Remaining limits:

- Explicit ordering, fixed dates, stable totals, and key uniqueness detect many changes, but EIA provides no snapshot-isolation guarantee. Same-count revisions during pagination may escape detection.
- A correct API total does not independently prove complete historical entity coverage. Per-entity lifecycle expectations are unknown; coverage checks only require at least one record per route per requested date.
- The facility total mismatch currently blocks publishing the tested real window. Investigate the upstream count semantics or a separately verified completeness strategy before accepting it or running larger windows.
- Intermittent connection-reuse timeouts remain unexplained. Fresh connections and bounded retries mitigate them; they do not guarantee EIA availability.
- Strict null/precision/schema-drift policies can reject legitimate historical variants; review evidence before changing those policies.
- Publication assumes local POSIX-style atomic rename and directory fsync (tested on macOS). Object storage/network filesystems and Windows are not validated.
- Uniqueness sets grow with the date window; this local implementation is not intended to hold full-history keys for arbitrarily large runs. No resume checkpoint, history merge, or snapshot garbage collection is implemented.
