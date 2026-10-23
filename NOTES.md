# Engineering Notes

## How I used AI

I used **Codex** to generate most of the application code and to assist with tests, code reviews, and documentation. I did not manually write the application code.

My role was to define the requirements, choose the technology stack, design the architecture, plan the implementation in incremental steps, direct Codex's work, review the results, investigate unexpected behavior, request corrections, and verify the application through automated checks and manual browser testing. I used AI-assisted reviews as one source of evidence, not as a substitute for checking whether the application behaved correctly.

The Git history reflects incremental development: environment setup, EIA exploration, extraction and recovery, analytical views, authentication and authorization, API endpoints, frontend features, recorded-data analysis, and subsequent corrections. I made the Git commits myself rather than having Codex commit automatically.

## AI-assisted implementation issues I identified and corrected

### 1. Recovery after the publication pointer changed

An early Codex-generated incremental refresh implementation could treat a new snapshot as unpublished if the process was interrupted immediately after replacing the authoritative `current.json` pointer. That risked inconsistent recovery state even though the publication pointer had changed.

I requested a critical review of the refresh implementation. That review identified the publication-boundary issue, and I directed the fix and requested a regression test. The [refresh implementation](apps/backend/src/outage_explorer/refresh_eia.py) now checks the authoritative pointer and validated snapshot metadata instead of relying only on an in-memory publication flag. Recovery also reconciles audit records with the published state.

The regression test [`test_interrupt_immediately_after_pointer_replace_is_published`](apps/backend/tests/test_refresh_eia.py) injects an interruption immediately after the pointer replacement and checks recovery of the published version, manifest, and watermarks, including an audit-mirror failure case.

The fix and test are included in commit **`0106423`**. The faulty version was identified in the working tree before it was committed; there is no separate commit containing that defect.

### 2. Facility count validation blocked live publication

The original ingestion policy required the number of returned records to equal the API's reported total for every route. Real EIA requests showed that the Facility route could return a valid terminal empty page and internally consistent facility records while still reporting a larger total. The strict check prevented publication of all three datasets, even though the received records reconciled across grains.

I investigated the mismatch, challenged whether the reported-total equality check was an appropriate universal requirement, and directed a narrowly scoped change rather than disabling completeness checks. The resulting **`facility_terminal_cross_grain_v1`** policy keeps strict reported/received equality for National and Generator. Facility can pass a reported-total mismatch only with stable pagination metadata, an explicit terminal empty page, valid unique records covering the requested dates, a reported total equal to the validated Generator count, and exact cross-grain key and MW reconciliation. The original counts and an explicit unverified-source-completeness warning remain in the publication metadata.

I verified this behavior with recorded-response tests and publication replay, then ran a live EIA refresh and checked the resulting snapshot, watermarks, and audit records. The change allows internally validated data to be published; it does **not** prove that EIA returned a complete source inventory. The evidence and limits are detailed in [FINDINGS.md](FINDINGS.md) and [DECISIONS.md](DECISIONS.md).

### 3. Misleading disabled-button behavior in the frontend

During manual browser review, I noticed that a disabled pagination button showed misleading cursor feedback. I raised the issue, had the styling corrected, and requested regression coverage. This was a user-interface issue rather than a data-integrity or authorization failure. It reinforced the value of checking interactive behavior in a real browser instead of relying only on component tests.

## How I verified the implementation

I checked more than whether the application started. I reviewed critical paths and used targeted tests, recorded EIA responses, synthetic failure cases, and manual browser checks to evaluate permissions, data consistency, publication, and recovery.

| Area | What I checked |
| --- | --- |
| **EIA extraction and Facility acceptance** | [Recorded-response tests](apps/backend/tests/test_facility_pagination_evidence.py) and [acceptance tests](apps/backend/tests/test_facility_acceptance.py) cover pagination, reported/received counts, key coverage, and exact MW comparisons. |
| **Refresh and recovery** | [Refresh tests](apps/backend/tests/test_refresh_eia.py) cover interrupted publication, corrupt audit files, schema changes, storage failures, overlap handling, and concurrent refresh attempts. |
| **SQL and authorization** | [Query tests](apps/backend/tests/test_query.py) reject unsupported SQL and unauthorized access paths. [Authentication tests](apps/backend/tests/test_auth.py) cover invalid, expired, and tampered credentials and role permissions. |
| **Demo isolation** | [Demo tests](apps/backend/tests/test_demo.py) verify the separate demo data and that demo refresh cannot modify production state. |
| **Reconciliation** | [Reconciliation API tests](apps/backend/tests/test_reconciliation_api.py) cover matching recorded data and synthetic missing records, small MW differences, and offsetting gaps. |
| **Findings** | [Offline analysis tests](apps/backend/tests/test_analysis_eia.py) reproduce the Facility count inconsistency, October 1 capacity change, and October 3 outage increase from saved EIA responses. |
| **Frontend** | [Exploration](apps/frontend/src/exploration.test.tsx), [reconciliation](apps/frontend/src/reconciliation.test.tsx), [fleet metric](apps/frontend/src/fleet-share.test.tsx), and [completion](apps/frontend/src/completion.test.tsx) tests cover UI states, permissions, pagination, formatting, refresh behavior, and guidance. |

Backend tests use mocked HTTP, recorded evidence, and synthetic fixtures where appropriate. Frontend tests use jsdom and mocked HTTP. Synthetic cases demonstrate that a check works; they are not presented as anomalies observed in EIA data.

### Automated verification

On October 22, a documentation-review run completed 69 focused tests, **536 backend tests**, and **80 frontend tests**, with successful frontend typecheck and production build. That run also checked 85 relative documentation links and anchors, the ER diagram, CLI commands, npm scripts, and the reproducibility of archived findings.

After subsequent ingestion changes, a later backend run completed **585 passing tests** with one existing Starlette TestClient deprecation warning. After the final Refresh form clarification, the frontend completed **98 passing tests**, along with `npm run typecheck`, `npm run build`, and `git diff --check`. These are recorded results from separate runs, not a claim that a clean installation of the final commit has already been tested.

A local-only Git secret-hygiene review inspected all 31 then-reachable commits, 200 unique file versions, tracked configuration, and decompressed evidence. It found no confirmed secrets or unresolved potential secrets. This pattern-based review does not guarantee detection of every possible credential.

The current test commands are in the [README](README.md#running-tests). The [setup review](docs/setup-review.md) records earlier environment checks and historical dependency advisories.

### Live EIA and production-backend verification — October 23, 2026

I ran the production incremental refresh against the live EIA API for **October 20, 2026**, using an isolated local data directory. The first run published **1 National, 55 Facility, and 95 Generator** observations. Facility reported 95 records but returned 55; the named acceptance policy allowed publication after the required validations. I checked the published snapshot, manifest, watermarks, run audit, and explicit source-completeness warning.

A second live refresh of the same date produced identical logical records and unchanged watermarks, without duplicate rows. It created a new immutable snapshot version, so this verifies **data-level idempotence**, not the absence of a new publication event.

I then ran the production FastAPI backend against that local snapshot and exercised the frontend through Vite's `/api` development proxy. I manually checked Admin, Analyst, and Viewer access, authorized datasets, pagination, SQL Explorer, fleet metrics, and refresh behavior. An Admin refresh request for a date already at the watermark completed as a successful **no-op**, and the browser showed the job as succeeded with no changes needed.

One initial browser refresh attempt failed because I supplied an explicit start date after a snapshot already existed. The backend correctly rejected that request without changing the published data. I subsequently verified the successful no-op and clarified the form label and help text so the first-publication requirement is easier to understand.

These were **local production-code integration checks**, not a public deployment or a full automated browser end-to-end suite. The 30-day analysis was reproduced separately from recorded responses; the live verification covered one day.

## Remaining limitations

- **Source completeness:** the Facility API's reported/returned count discrepancy remains unexplained. The acceptance policy verifies the received observations and records the uncertainty; matching totals do not certify complete source coverage.
- **Source findings:** the three findings consist of one verified API count inconsistency and two unusual, internally consistent observations. They are not three proven source-data errors.
- **Durability:** interruption tests cover specific simulated failures, not every possible power-loss or filesystem failure. In-process refresh job status is not restart-durable.
- **Testing scope:** FastAPI integration tests, jsdom tests, and manual browser checks do not replace a clean-environment deployment test or automated browser end-to-end tests.
