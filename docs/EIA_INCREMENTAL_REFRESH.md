# EIA ingestion, incremental refresh, and recovery

This is the operational runbook for both cumulative refresh and independent date-window extraction. See [implemented schemas](DATA_MODEL.md), [observed wire metadata](EIA_SCHEMA.md), and [real-data findings](../FINDINGS.md).

The refresh command reuses `extract_eia.extract_route`, including its strict date, numeric, natural-key, schema, pagination, total, and daily-coverage checks. It adds cumulative merging, per-route watermarks, run audits, and one atomic publication across all three routes. There are no new dependencies or distributed services.

**The [documented facility discrepancy](EIA_PAGINATION_INVESTIGATION.md) is not bypassed.** Recorded responses with 55 rows against `total=95` still fail before publication. These commands describe the workflow, not evidence of a successful live refresh. Incremental success/recovery is tested offline; a successful live production refresh has not been demonstrated. The separate recorded source investigations are historical evidence, not accepted publications.

## Commands from the repository root

Install the backend and configure `EIA_API_KEY` in `apps/backend/.env` or the environment as before. The existing secure loader is reused; credentials and HTTP request URLs are not stored in run history or logs.

Bootstrap a dedicated cumulative dataset with an explicit first window:

```sh
apps/backend/.venv/bin/python -m outage_explorer.refresh_eia \
  --start 2026-10-18 --end 2026-10-20
```

Then derive the next request window from the successfully published snapshot:

```sh
apps/backend/.venv/bin/python -m outage_explorer.refresh_eia \
  --end 2026-10-21 --overlap-days 2
```

If that overlap contains actual source corrections, inspect the failure and explicitly authorize incoming values to replace matching stored records:

```sh
apps/backend/.venv/bin/python -m outage_explorer.refresh_eia \
  --end 2026-10-21 --overlap-days 2 --conflict-policy replace
```

A successful legacy window snapshot can be adopted explicitly into a new cumulative root:

```sh
apps/backend/.venv/bin/python -m outage_explorer.refresh_eia \
  --base-snapshot data/eia/2026-10-18_2026-10-20 \
  --end 2026-10-21 --overlap-days 2
```

The base argument names a directory containing `current.json`, not an arbitrary/orphan run directory. Its actual schemas, values, key uniqueness, row counts, coverage, and key definitions are checked before adoption. Legacy version-1 watermarks are derived from validated row maxima. The base remains unchanged. The command does not guess which legacy window is authoritative by scanning directories. `--base-snapshot` is rejected once the cumulative root has its own publication; `--start` is rejected when a current/base snapshot exists.

The original `extract_eia` command still produces independent window snapshots. It is not silently converted into cumulative extraction.

## Options and planning

- `--output-dir`: defaults to `data/eia/incremental`. Use a dedicated local directory and only the refresh command to update it.
- `--end`: required, inclusive ISO date. It cannot precede the current watermark.
- `--start`: required only for a first run without a base snapshot.
- `--overlap-days`: defaults to 7; allowed range 0–3660. This counts previously published dates, including the watermark date. All newly requested dates through `--end` are also fetched.
- `--conflict-policy`: `reject` (default) or explicit `replace`.
- `--env-file`, `--page-size`, `--max-pages`, `--attempts`, `--backoff`, and `--timeout` use the [shared extraction options and bounds](#shared-extraction-options-and-validation). Retry/page limits and total consistency checks are unchanged.

For dataset start `S`, latest watermark `W`, and overlap `N`, the computed start is `max(S, W + 1 day - N days)`. For example, after publishing through October 20, overlap 0 requests October 21 onward; overlap 1 starts October 20; overlap 2 starts October 19. Larger overlaps clip to the first stored date.

All three route watermarks must match the stored maxima and manifest end. Disagreement is an integrity failure, not permission to choose a convenient watermark. A watermark represents successfully published data only, never the maximum date merely downloaded or staged.

With overlap 0 and `end == W`, the command performs a validated, audited `no_op`: no HTTP requests, pointer change, or watermark advancement. That audit has no requested start (no extraction window), and records the computed `next_start`. With positive overlap, the same end date can be refreshed repeatedly.

## Merge policy

Rows merge on the provisional keys already documented in [EIA_SCHEMA.md](EIA_SCHEMA.md): `period`; `(period, facility)`; and `(period, facility, generator)`.

1. Duplicates within any incoming page/window or stored dataset always fail, even when values are identical. `replace` does not permit ambiguous duplicates.
2. A matching key with an identical typed row is kept once. This explicitly defined overlap rule makes repeat refreshes logically idempotent.
3. A matching key with different values fails under the default `reject` policy. Under explicit `replace`, the entire validated incoming row wins, including descriptive-name corrections. No invalid values are coerced to make a conflict disappear.
4. New keys insert. Existing rows outside the request window remain unchanged.
5. If an old key inside the overlap disappears from the refreshed result, the run fails under either policy. Source deletion, missing coverage, and source-data errors cannot be distinguished reliably here; no implicit deletion/tombstone policy is implemented.

Every incoming route must first pass the existing strict extraction checks. A correct-looking merge cannot rescue incomplete pagination, changing totals, empty routes, or missing dates. The 55-versus-95 facility fixture fails under both conflict policies. Totals are checked for self-consistency, not assumed to prove authoritative source coverage.

Each cumulative route keeps the existing fixed Arrow schema: daily dates, string identifiers, nullable display name, exact decimal measures, and explicit units. Schemas and rows are read from the physical Parquet file with `ParquetFile`, so Hive-style ancestor names such as `source=eia` do not inject partition fields. Existing schemas are compared exactly, including field order, nullability, precision, and metadata; there is no automatic cast or migration. Row values and keys are revalidated too. Incompatible source facets, units, row fields, dates, values, or stored schema versions fail. Additional unrequested measure definitions in metadata are not automatically added to the stored projection.

## Output and atomic publication

```text
data/eia/incremental/
  .refresh.lock
  current.json                         # {"run": "runs/<version>"}
  ingestion-runs/<run-id>.json          # Durable run audit, including failures
  runs/<version>/
    manifest.json                      # Schema v2, watermarks, embedded run audit
    us-nuclear-outages.parquet
    facility-nuclear-outages.parquet
    generator-nuclear-outages.parquet
```

Temporary `.staging-*` directories and temporary JSON files are not reader-visible publications. Default output and audit files live under Git-ignored `data/`.

A local nonblocking POSIX writer lock covers base selection, merging, and publication. Another refresh fails with its own audit instead of calculating from a stale snapshot and losing a writer's updates. Old immutable snapshots are retained. Different writers must use the same output root to share this lock; manually editing files or using unrelated writers is unsupported.

The new three-route snapshot is validated, flushed, and fsynced before its run directory is renamed into place. **Only replacing `current.json` commits publication.** Watermarks and the completed run audit are embedded in that immutable manifest, so data, all three watermarks, and the authoritative success metadata change together. There is no separately updated watermark file to get ahead of the data.

Resolve the pointer once for a consistent read:

```python
import json
from pathlib import Path
import pyarrow.parquet as pq

root = Path("data/eia/incremental")
run = root / json.loads((root / "current.json").read_text())["run"]
manifest = json.loads((run / "manifest.json").read_text())
watermarks = manifest["watermarks"]
generators = pq.ParquetFile(run / "generator-nuclear-outages.parquet").read()
```

Do not glob across versions or combine overlapping legacy windows indiscriminately. Idempotency refers to the current logical datasets; each successful refresh has a new immutable version and audit even when its data are unchanged.

## Ingestion run metadata and failures

Each engine attempt gets a unique run ID and a durable audit. Metadata includes:

- UTC start and finish timestamps, status, overlap and conflict policy.
- Computed requested start/end, base snapshot version, and published snapshot version (null when nothing was published).
- Per-route status; previous, fetched, merged, inserted, unchanged, and replaced row counts as applicable; before/after watermarks.
- Safe failure reasons and partial progress. `fetched_rows` counts fully validated source pages; an invalid/rejected page is not included in that progress counter. Row/page location errors are retained without raw values or credential-bearing URLs.

Statuses are `running`, `published`, `failed`, `interrupted`, `incomplete`, and `no_op`. Per-route `validated` means staged/checked, not published. A run that fails on one route never advances another route's watermark. Ordinary exceptions and SIGINT before publication record failure/interruption and clean staging. An exception or SIGINT after pointer replacement cannot undo publication; its exit status can still indicate failure/interruption. Storage errors abort immediately; route validation failures are accumulated so other routes can be diagnosed without partial publication.

The success record is prepared after validation and embedded in the immutable manifest before pointer commit. Its presence in an **unreferenced** run directory does not prove publication. An audit mirror is updated after commit. If an interruption occurs immediately after pointer replacement, the exception handler checks the actual pointer and validates its snapshot rather than trusting an in-memory flag. A committed run retains its published status, version, row counts, and after-watermarks. If mirror persistence fails, the referenced manifest remains authoritative and an audit-update warning is logged.

At the next refresh, recovery runs under the writer lock after validating the current pointer, physical datasets, manifest watermarks, and embedded publication metadata (including route statuses, merged counts, and after-watermarks). The current run's mirror is restored from that manifest regardless of whether the mirror is missing, `running`, `interrupted`, `failed`, or contradictory `published` metadata. Corrupt current pointers, manifests, datasets, or embedded publication metadata abort the refresh; they are never quarantined or bypassed.

Malformed historical audit JSON/encoding, invalid basic audit structure, and contradictory current mirrors are preserved byte-for-byte as `ingestion-runs/<original-name>.<uuid>.corrupt`. Recovery emits `audit_evidence_preserved` with the evidence filename, without logging its contents. Unrelated corrupt records no longer block valid refreshes; the evidence remains for manual inspection and is not scanned again as an audit. A valid current mirror is recreated from the validated manifest. Other abandoned `running` records become `incomplete`, preserving existing failure reasons. If preserving evidence or writing a replacement fails, the refresh aborts before extraction. Recovery repairs metadata only; it does not advance watermarks or infer publication from orphan directories.

Before any network request, the engine creates missing output ancestors and the audit directory and fsyncs their parent directory entries, then writes/fsyncs the initial audit and its containing directory. File and directory fsync failures stop extraction. Snapshot files, the snapshot directory, and the runs directory are synced before pointer replacement; the output directory is synced after replacement. This ordering addresses the missing initial-directory durability barrier on a local POSIX filesystem. Directory setup failures can occur before an audit can be created.

SIGKILL/power loss can still leave a `running` mirror and unreferenced files until recovery. An atomic rename gives readers an all-or-nothing pointer change; persistence across power loss additionally depends on successful fsync and the filesystem/device honoring it. A crash between pointer rename and its directory fsync can recover either pointer; only the validated current pointer determines publication. Tests inject interruptions/storage failures and assert sync ordering; they do not simulate actual power loss or establish guarantees for every filesystem, OS, device cache, or hardware failure. Storage unavailable during audit creation cannot provide a durable run record. Invalid CLI syntax/date parsing precedes an engine run. A post-commit storage error can produce a failing exit status despite a complete current snapshot; readers and recovery use the pointer/manifest rather than process exit alone.

## Offline validation and remaining limits

```sh
apps/backend/.venv/bin/python -m pytest apps/backend/tests
```

Tests use MockTransport or recorded public responses. They cover bootstrap, explicit legacy adoption, computed overlap and next-day windows, unchanged repeats, opt-in corrections, missing/duplicate keys, metadata/Parquet incompatibility, stored watermark consistency, no-op behavior, concurrent writer rejection, HTTP/disk/interruption failures, stale-run recovery, audit-write gaps after publication, post-pointer-replacement interruption with and without mirror-write failure, malformed/contradictory/missing mirrors, preserved historical corruption, rejection of corrupt current publication metadata, Hive-style paths, initial-directory sync ordering and failures, and the unchanged real facility-count regression.

Limits remain explicit:

- The EIA facility count discrepancy still blocks the known real window. No watermark is fabricated to get past it.
- Natural keys remain provisional across history. Stable totals and daily coverage do not establish complete historical entity coverage or API snapshot isolation.
- Overlap only revisits recent stored dates. Older corrections require a separately planned workflow; backward watermark changes and implicit deletions are rejected.
- Merging validates and rewrites the entire cumulative snapshot in local memory. This is appropriate for small take-home runs, not an arbitrarily large history service.
- Locking, rename, and directory fsync assume a local POSIX filesystem. Windows, object storage, and unreliable network filesystems are not supported.
- No scheduler, automated historical backfill, schema migration, old-run garbage collection, or distributed coordination was added.

## Shared extraction options and validation

Configure `EIA_API_KEY` in `apps/backend/.env` or the environment. Exported values take precedence; interpolation is disabled. The default env path is resolved from the module location, not the working directory. Keys are never command-line arguments or logged. Both commands use HTTPX, PyArrow, and python-dotenv already declared by the backend.

| Option | Default | Bounds / behavior |
| --- | --- | --- |
| `--page-size` | 500 | 1–5000 rows per request |
| `--max-pages` | 10000 | 1–100000 per route; exhaustion fails without publication |
| `--attempts` | 4 | 1–6 total attempts per request, including the first |
| `--backoff` | 1 second | Finite 0–30; exponential waits capped at 30 seconds |
| `--timeout` | 30 seconds | 1–60 seconds per HTTP operation |
| `--env-file` | `apps/backend/.env` | Explicit module-relative default |
| `--output-dir` | Refresh: `data/eia/incremental`; windows: `data/eia` | Separate local roots supporting atomic rename |

All three routes use fixed inclusive start/end dates, all three known measures, and ascending `period`, then `facility`/`generator` where applicable. Offset advances by actual received row count. A short page does not imply completion while the reported total says rows remain. Totals must be stable and equal received rows; empty routes, early empty pages, oversize/malformed pages, duplicate candidate keys (including identical duplicates), and missing route-days fail. This conservative acceptance policy is not independent authoritative source-coverage certification.

Metadata must advertise daily data, known dimensions, measures, and expected units; response frequency/date format and API-version consistency are checked. Unexpected row fields require review. The [physical schema and validation rules](DATA_MODEL.md#invalid-records-duplicates-and-relationships) retain exact dates, opaque IDs, optional names, non-null decimal128(24,6) measures, and explicit units. Native decimal JSON numbers, integers, and decimal strings avoid intermediate float conversion. Null/blank/nonfinite/unrepresentable measures fail rather than rounding or zero-filling; finite negative values are retained pending source interpretation. Names may be absent/null without changing identity.

Validation/writing is page by page, with key/date sets in memory. Invalid rows reject the whole snapshot and are located by route/page offset/row index with a safe reason; raw values are not retained in a rejection file. Required units are `megawatts` for capacity/outage and `percent` for percentOutage. The fixed source field names and non-null unit columns are documented in the [model](DATA_MODEL.md#grains-exact-types-and-source-mapping).

Only transient network errors/timeouts, HTTP 429, and HTTP 5xx are retried. Default delays are 1, 2, and 4 seconds. Retry-After seconds/HTTP dates are honored within the 30-second delay cap; longer server waits are clamped and sustained throttling can exhaust the budget. Authentication errors, redirects, malformed JSON/envelopes, warnings, validation errors, and completeness failures are not automatically retried. Fresh HTTP connections mitigate unexplained intermittent reuse timeouts; TLS verification stays enabled.

JSON-line stderr logs report windows, page counts/totals, retries/delays, failures, elapsed time, and publication. They exclude request URLs, keys, raw rows, and remote error bodies; HTTPX/HTTPCore logging is disabled. CLI exits are 0 for successful completion (including refresh no-op), 1 for failure, and 130 for interruption. Errors after pointer commit can leave a complete snapshot current despite a failing exit; inspect the authoritative pointer/metadata rather than infer rollback from exit status.

## Independent date-window extraction

This legacy workflow remains separate from cumulative refresh. Run from the repository root after installing the backend and configuring credentials. Start with a small window; no full-history download has been run.

```sh
apps/backend/.venv/bin/python -m outage_explorer.extract_eia \
  --start 2026-10-20 --end 2026-10-20 \
  --page-size 25 --attempts 3 --timeout 20
```

`--start` and `--end` are required inclusive daily ISO dates. The historical example currently fails facility completeness, as documented in [FINDINGS.md](../FINDINGS.md#finding-1--facility-api-count-inconsistency); it is not a working-demo startup command.

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

A successful schema-version-1 manifest records inclusive dates, creation time, API versions, keys, page/record counts, and relative filenames, without credentials/request echoes. Parquet uses Zstandard compression; footers and Arrow schemas are checked before publication.

All three files and the manifest are staged, closed, checked, and fsynced. A unique staging directory is renamed to an immutable run; a flushed temporary pointer is atomically replaced over `current.json`. Readers resolve that pointer once. Pre-publication exceptions preserve existing data. Route validation failures are accumulated to diagnose other routes; storage errors/interruption abort immediately. Post-publication storage errors/signals cannot undo the committed pointer.

Repeat runs replace the current logical dataset without appending rows; unchanged sources therefore remain logically idempotent, although a new complete version is retained. Independent-window concurrent runs have unique staging/run/pointer names and last-successful-publisher semantics; they do not use the cumulative refresh lock. Different or overlapping windows are independent, not a combined history. Never use this writer to update the cumulative root or glob across runs/windows.

After a successful window, trusted local code can read the physical Parquet file:

```python
import json
from pathlib import Path
import pyarrow.parquet as pq

window = Path("data/eia/2026-10-20_2026-10-20")
run = window / json.loads((window / "current.json").read_text())["run"]
national = pq.ParquetFile(run / "us-nuclear-outages.parquet").read()
```

Ordinary exceptions/SIGINT clean staging; SIGKILL/crashes or failures between run rename and pointer replacement can leave unreferenced staging/orphan runs. Readers ignore them and old successful runs are retained. There is no garbage collection, window resume checkpoint, or automatic history merge. Candidate-key sets grow with the window. Local output is Git-ignored. Neither stable totals nor route-day coverage proves entity lifecycle coverage; same-count source revisions during pagination may escape detection. Review legitimate historical null/precision/schema variants before changing strict policies.

## Diagnostic collection and demonstration

The [schema probe](EIA_SCHEMA.md#evidence-and-reproduction), [dated pagination investigation](EIA_PAGINATION_INVESTIGATION.md), and [30-day findings/replay](../FINDINGS.md#offline-reproduction) retain the historical live results and regression evidence. They do not authorize production acceptance. For the working isolated app, use the [README demo commands](../README.md#quick-start--working-real-data-demo), not either production ingestion command. Demo preparation writes separate `demo.json`/Parquet files, no production pointers/audits/watermarks, and demo refresh fails read-only.

If a separately authorized new diagnostic collection is necessary, use a fresh filename under `eia-analysis` (these commands make live requests):

```sh
apps/backend/.venv/bin/python -m outage_explorer.analysis_eia fetch \
  --output data/eia-analysis/new-run/source.json.gz
apps/backend/.venv/bin/python -m outage_explorer.analysis_eia fetch \
  --resume-from data/eia-analysis/new-run/source.json.gz \
  --output data/eia-analysis/new-run/resumed-source.json.gz
```

Optional `--end YYYY-MM-DD` fixes the end; otherwise the collector uses the minimum advertised latest date across routes. Start is always 29 days earlier. Resume validates the old archive/window and preserves it, fetching missing continuation pages into a fresh archive. The collector is isolated from production; do not represent its received population as certified complete.
