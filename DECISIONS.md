# Technical decisions

These seven decisions describe the implemented system. The [data model and ER diagram](docs/DATA_MODEL.md) define the schemas; [findings](FINDINGS.md) separate observed evidence from source-completeness claims.

## 1. Natural keys retain each source grain

**Choice:** date; date/facility; date/facility/generator, enforced in application validation and scoped to one selected snapshot.

**Rejected alternative:** surrogate row IDs or a globally unique generator ID.

**Rationale:** natural keys allow explainable duplicate detection and overlap merging without erasing grain or opaque identifiers. They depend on provisional source identity/lifecycle assumptions, so full-history uniqueness is not claimed.

Implementation: [source](apps/backend/src/outage_explorer/extract_eia.py).

## 2. Current means the atomic published snapshot; refresh overlaps watermarks

**Choice:** immutable Parquet runs selected by one current pointer, per-route watermarks embedded with version-2 data/audit, and bounded configurable overlap with default conflict rejection.

**Rejected alternative:** append-only files with independent route/watermark updates, or a last-success timestamp separate from publication.

**Rationale:** readers cannot mix partial routes or advance freshness ahead of data, and overlap corrections are explicit/idempotent. Full cumulative merge/validation rewrites use memory and disk proportional to history; readers do not pin across separate HTTP requests.

Implementation: [source](apps/backend/src/outage_explorer/refresh_eia.py).

## 3. Preserve orphans and explain cross-grain mismatches

**Choice:** enforce route-local integrity, retain orphan generators, and full-outer compare preaggregated children with parents; report signed gaps without forced balancing.

**Rejected alternative:** inferred physical foreign keys, dropping unmatched children, or reconstructing a parent total to hide differences.

**Rationale:** source definitions and lifecycle coverage are not fully established, so comparison evidence must not invent a master inventory. Orphans/gaps can remain visible; not every mismatch is a proven defect or automatic extraction failure. This does not relax route pagination completeness.

Implementation: [source](apps/backend/src/outage_explorer/diagnostics.py).

## 4. Refresh is asynchronous within one API process

**Choice:** Admin-only in-process background worker, 202 admission, polling by job ID, and single-active-job admission plus the pipeline filesystem lock.

**Rejected alternative:** blocking long HTTP refresh requests or adding Celery/Redis/brokers.

**Rationale:** bounded local infrastructure suits slow EIA requests while existing snapshots remain readable. API jobs are not durable across restarts, graceful shutdown may wait, and deployment requires a single API worker; durable ingestion audits are distinct from job status.

Implementation: [source](apps/backend/src/outage_explorer/refresh_jobs.py).

## 5. SQL is a compiled, deliberately small read-only subset

**Choice:** SQLGlot AST validation and reconstruction of one SELECT on one approved view, plain column/star projections, constrained predicates, ORDER BY, and LIMIT.

**Rejected alternative:** executing raw SQL after a regex/prefix check, or accepting broad DuckDB SELECT syntax.

**Rationale:** rejecting writes, multiple statements, CTEs, subqueries, joins, unions, functions/table functions, qualified tables/columns, aliases, external scans, PRAGMA, ATTACH, COPY, and unknown nodes closes resource-access paths. Literals become bound parameters. The server fetches at most 201 and returns at most 200 rows with truncation status; API SQL length is capped at 10,000 characters and AST size at 512 nodes. Aggregation and advanced SQL are intentionally unavailable; result limits do not bound all execution work or provide a query timeout.

Implementation: [source](apps/backend/src/outage_explorer/api/query.py).

## 6. Every accepted table reference is authorized before data access

**Choice:** require Analyst/Admin first, structurally require exactly one SELECT and one Table node in the full AST, accept only an unqualified canonical table from `DATASETS`, then invoke centralized `authorize_datasets` before `open_analytics`. Every unrecognized construct fails closed rather than escaping table discovery.

**Rejected alternative:** checking only the first textual FROM, trusting a client dataset list/frontend hiding, or accepting nested SQL with incomplete reference authorization.

**Rationale:** the accepted subset has one exhaustively checked reference; nested/indirect references cannot enter execution. Viewer cannot use `/query` even for national data; catalog/preview and the national metric separately apply dataset policy. Broader future SQL must extend AST authorization first. Trusted local Python/DuckDB helpers themselves are not an authorization sandbox.

Implementation: [source](apps/backend/src/outage_explorer/auth.py).

## 7. Preserve exact values and separate evidence from acceptance

**Choice:** non-null fixed Decimal storage for measures, explicit units, strict schema/record validation, nullable labels, and non-production archives for unverified source analysis.

**Rejected alternative:** floating-point coercion, zero-filling invalid measures, automatic schema widening, or replacing an inconsistent EIA total with the observed key count.

**Rationale:** exact gaps and reproducible evidence survive without silent rounding or false completeness. The storage policy rejects otherwise numeric data needing greater precision; the facility mismatch blocks useful production data until its semantics are independently resolved. An internally consistent archive or a coordinated capacity change is not automatically a complete publication or a source anomaly.

Implementation: [source](apps/backend/src/outage_explorer/extract_eia.py).
