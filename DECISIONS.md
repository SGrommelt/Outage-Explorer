# Engineering Decisions

These are seven key engineering decisions behind Outage Explorer. Each includes the chosen approach, a rejected alternative, and the reasoning behind the trade-off. The [data model](docs/DATA_MODEL.md) has the detailed schemas and ER diagram, while [FINDINGS.md](FINDINGS.md) covers what I observed in the EIA data.

## 1. Natural keys for each dataset

**Choice:** I use `outage_date` for national records, `(outage_date, facility)` for facility records, and `(outage_date, facility, generator)` for generator records. I validate uniqueness within each dataset and published snapshot.

**Alternative rejected:** Assigning a new row ID or assuming generator IDs are globally unique.

**Why:** These keys match the level of detail in each EIA route. They let me identify duplicates and merge overlapping refresh periods without losing the source identifiers. I treat the identity rules as provisional, since I haven't verified them against the full EIA history.

**Implementation:** [Extraction and validation](apps/backend/src/outage_explorer/extract_eia.py).

## 2. Keeping the data current

**Choice:** A refresh builds and validates a complete, immutable Parquet snapshot for all three routes before switching a single pointer to it. I track a watermark for each route and re-fetch a configurable overlap window to catch recent corrections. Conflicting values are rejected by default rather than silently overwritten.

**Alternative rejected:** Appending new rows to each dataset independently and updating a separate “last refreshed” timestamp.

**Why:** I wanted readers to see either the previous complete snapshot or the new one, never a mix of partially updated datasets. Watermarks advance with the published data, not before it. The trade-off is that merging and validating the full accumulated history becomes more expensive as the dataset grows, and separate API requests are not guaranteed to read the same version.

**Implementation:** [Incremental refresh and publication](apps/backend/src/outage_explorer/refresh_eia.py).

## 3. Missing facilities and reconciliation differences

**Choice:** Before publishing new data, I validate matching facility/date relationships and exact capacity and outage totals across the three freshly fetched datasets. Missing relationships or mismatched totals block publication; records are never dropped or adjusted to force agreement. The analytical reconciliation tools can still report discrepancies in existing data and test fixtures.

**Alternative rejected:** Dropping generators without a matching facility or adjusting one dataset's totals to make the numbers agree.

**Why:** I do not have enough evidence to treat the Facility route as an authoritative master list. Removing records or forcing totals to match would hide the inconsistencies the analysis is meant to reveal. A difference deserves investigation, but does not automatically prove the source is wrong. This conservative publication rule may reject legitimate differences in how EIA defines the datasets, and passing it does not establish complete source coverage.

**Implementation:** [Reconciliation and diagnostics](apps/backend/src/outage_explorer/diagnostics.py) and [publication validation](apps/backend/src/outage_explorer/refresh_eia.py).

## 4. Asynchronous refresh

**Choice:** Refresh runs as an Admin-only background job in the API process. The request returns `202` with a job ID, and the frontend polls for status. Only one refresh is admitted at a time, with an additional filesystem lock protecting the pipeline.

**Alternative rejected:** Keeping the HTTP request open until refresh finishes, or introducing a queue and worker infrastructure such as Celery and Redis.

**Why:** EIA requests can take time, and I didn't want the UI to block or existing data to become unavailable during ingestion. An in-process job is sufficient for a local challenge without adding infrastructure. Its limitation is that API job status does not survive a restart, so the API must run with one worker; the pipeline's on-disk audit records are separate from that temporary job status.

**Implementation:** [Refresh job management](apps/backend/src/outage_explorer/refresh_jobs.py).

## 5. Supported SQL and its limits

**Choice:** I use SQLGlot to parse SQL and allow only a restricted, read-only `SELECT` against one approved dataset. Queries can select columns or `*`, apply supported `WHERE` filters, sort with `ORDER BY`, and use `LIMIT`. Values are passed as bound parameters, and results are capped at 200 rows.

**Alternative rejected:** Checking whether a query starts with `SELECT` and then passing the original SQL directly to DuckDB, or allowing unrestricted DuckDB queries.

**Why:** A query that looks read-only can still access files, invoke functions, or reference data the user should not see. I validate the parsed structure and rebuild only the supported query shape. Joins, subqueries, CTEs, unions, functions, aliases, aggregation, multiple statements, writes, and external access are rejected. I also cap input length at 10,000 characters and the parsed tree at 512 nodes. This intentionally limits what an Analyst can do; the 200-row result cap is not a substitute for a query execution timeout.

**Implementation:** [SQL query API](apps/backend/src/outage_explorer/api/query.py).

## 6. Finding and authorizing every table in a query

**Choice:** Before opening the analytical data, I check that the user has the Analyst or Admin role, parse the entire statement, and require exactly one `SELECT` with one table reference. That table must be an unqualified name in the approved `DATASETS` catalog. I then call the centralized dataset authorization check. Any unsupported structure is rejected.

**Alternative rejected:** Looking only at the first `FROM` in the SQL text or trusting the frontend to hide unauthorized datasets.

**Why:** Text matching can miss tables referenced through nested queries or other SQL constructs. Because my supported SQL subset allows exactly one table and rejects nested or indirect references, I can account for every table the accepted query touches before it reaches DuckDB. If I expand SQL support later, table discovery and authorization will need to expand first. The internal Python/DuckDB helpers are trusted code, not a substitute for API authorization.

**Implementation:** [Authorization policy](apps/backend/src/outage_explorer/auth.py) and [SQL validation](apps/backend/src/outage_explorer/api/query.py).

## 7. Exact numeric values and cautious source acceptance

**Choice:** I store measurements as fixed-precision decimals, validate their types and units, and keep optional descriptive labels nullable. Production uses the named `facility_terminal_cross_grain_v1` acceptance policy; the read-only archive/demo remains separate. National/Generator retain reported-total equality; Facility requires terminal traversal and cross-grain validation. A count exception additionally requires Facility reported total to equal validated Generator received count.

**Alternative rejected:** Using floating-point values for reconciliation, replacing missing or invalid measurements with zero, silently accepting schema changes, or enforcing reported/received row-count equality for Facility even when independently validated records are available.

**Why:** Small rounding differences matter when reconciling national, facility, and generator totals, and silently changing invalid values would undermine the analysis. Strict reported/received row-count equality initially prevented publication of otherwise validated Facility records because the EIA API reported more rows than it returned. The narrower Facility policy accepts that specific discrepancy only after terminal pagination and cross-grain checks pass. Both counts and the acceptance evidence remain in manifests and audits; the reported total is never rewritten. Published watermarks indicate accepted dates, not independently certified source coverage. Matching totals are useful validation evidence, but cannot prove the source is complete.

**Implementation:** [Extraction and validation](apps/backend/src/outage_explorer/extract_eia.py).
