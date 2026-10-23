# Engineering Notes

## How I used AI

I used **Codex** for most of the application code, as well as assistance with testing, code reviews, and documentation. I did not manually write the application code.

My role was to define the requirements, select the technology stack, design the system architecture, establish the implementation strategy, and break down the development process into incremental steps. I directed the implementation, reviewed the generated code, identified issues requiring further investigation, requested corrections and improvements, and verified the results through tests and manual browser testing.

The Git history reflects this incremental development process, including environment setup, EIA exploration, data extraction and recovery, analytical views, authentication and authorization, API endpoints, frontend features, and recorded-data analysis.

## An AI-generated mistake I caught and corrected

One important issue came from the initial Codex-generated incremental refresh implementation. If the process was interrupted immediately after replacing the authoritative `current.json` pointer, the code could incorrectly treat the new snapshot as unpublished. That left a risk of inconsistent recovery state even though the publication pointer had already changed.

I requested a critical review of the refresh implementation. The review identified this publication-boundary problem, and I requested a fix and a regression test to verify the behavior.

The [updated refresh implementation](apps/backend/src/outage_explorer/refresh_eia.py) now checks the authoritative pointer and validated snapshot metadata to determine whether publication occurred, rather than relying only on an in-memory flag. Recovery also reconciles the audit records with the published state.

The regression test, [`test_interrupt_immediately_after_pointer_replace_is_published`](apps/backend/tests/test_refresh_eia.py), replaces the real pointer and injects an interruption immediately afterward. It checks that the published version, manifest, and watermarks are recovered correctly, including when an audit mirror write fails.

The fix and test are included in commit **`0106423`**. The original faulty version was caught in the working tree before being committed, so there is no separate Git commit containing that defect. The committed test and implementation show the correction and how I verified it.

## How I verified the implementation

I did more than check whether the application started. I reviewed the critical paths and used targeted tests to check failure cases, permissions, data consistency, and recovery behavior.

| Area | What I checked |
| --- | --- |
| **EIA extraction** | [Recorded-response tests](apps/backend/tests/test_facility_pagination_evidence.py) check pagination counts, key coverage, and exact MW totals. The unresolved facility count issue is documented in [FINDINGS.md](FINDINGS.md). |
| **Refresh and recovery** | [Refresh tests](apps/backend/tests/test_refresh_eia.py) cover interrupted publication, corrupted audit files, schema changes, storage failures, and concurrent refresh attempts. |
| **SQL and authorization** | [Query tests](apps/backend/tests/test_query.py) reject unsupported SQL and unauthorized access paths. [Authentication tests](apps/backend/tests/test_auth.py) cover invalid, expired, and tampered credentials, along with role permissions. |
| **Demo isolation** | [Demo tests](apps/backend/tests/test_demo.py) check the separate demo data, role-based API behavior, and that demo refresh cannot modify production state. |
| **Reconciliation** | [Reconciliation API tests](apps/backend/tests/test_reconciliation_api.py) cover matching recorded data and synthetic cases with missing records, small MW differences, and offsetting gaps. |
| **Findings** | [Offline analysis tests](apps/backend/tests/test_analysis_eia.py) reproduce the facility count inconsistency, October 1 capacity change, and October 3 outage increase from the saved evidence. |
| **Frontend** | [Exploration](apps/frontend/src/exploration.test.tsx), [reconciliation](apps/frontend/src/reconciliation.test.tsx), and [fleet metric](apps/frontend/src/fleet-share.test.tsx) tests cover API-driven states, filters, pagination, formatting, and missing values. |

The backend tests use mocked HTTP, recorded evidence, and synthetic fixtures where appropriate. Frontend tests use jsdom and mocked HTTP. Synthetic cases demonstrate that a check works; they are not presented as anomalies found in EIA's real data.

### Recorded verification run — October 22, 2026

During the documentation review, I ran 69 focused tests followed by the full backend suite (**536 passed**, with one existing Starlette TestClient deprecation warning). The frontend suite passed **80 tests**, and both `npm run typecheck` and `npm run build` succeeded.

I also checked 85 relative documentation links and anchors, the seven required entries in `DECISIONS.md`, the ER diagram, CLI commands, npm scripts, and `git diff --check`. I reproduced archive counts and temporal examples offline and verified that the documentation-only review had not changed the 82 tracked non-Markdown files.

These numbers describe **that specific verification run**, not necessarily the latest test totals after subsequent changes. The commands to run the current suites are in the [README](README.md#running-tests). The [setup review](docs/setup-review.md) contains earlier environment checks and historical dependency advisories.

### Manual browser review

I also tested the application myself in a browser, including the latest changes, and reviewed the resulting behavior. This was manual verification, separate from the automated backend and frontend suites. I did not set up an automated browser end-to-end test suite.

## Remaining limitations

- **Live EIA publication:** production now allows the narrow Facility discrepancy under a terminal-traversal/count/cross-grain policy with durable unverified-source disclosure. Recorded-response publication was verified offline. I have not claimed a successful live production refresh. The read-only demo uses recorded observations and intentionally does not publish production data.
- **Source findings:** the three findings are one confirmed API count inconsistency and two unusual but internally consistent observations. They do not establish three source-data errors. Matching totals also cannot prove that the source returned every expected record.
- **Durability:** the interruption tests cover specific simulated failure points and recovery behavior, not every possible power-loss or filesystem failure.
- **Testing scope:** FastAPI integration tests and jsdom tests do not replace automated end-to-end browser tests or guarantee that a new environment will behave identically.

