# Engineering Notes

## AI assistance and attribution

Codex and ChatGPT assisted with implementation, tests, reviews, and documentation. This disclosure is based on the developer's instructions and development conversation; Git author names alone do not establish which lines were written manually or by AI. No claim is made that the developer personally wrote every implementation or discovered every defect.

The tracked history through `bbe1678` contains 20 incremental commits, progressing from setup and source exploration through ingestion/recovery, analytics, authentication/RBAC, APIs, frontend, real evidence, isolated demo, reconciliation, and metric polish. The repository is not shallow. These commits and recorded fixtures substantiate an iterative workflow; they are not a complete prompt transcript or proof of human/AI authorship for each step.

**TODO — developer confirmation:** identify which design choices, code edits, and reviews were performed manually, and which AI-generated work was materially revised. Add only personally confirmed activities and concrete examples.

## Concrete AI mistake — confirmation required

**TODO — required before claiming this Engineering Notes criterion complete:** confirm one specific AI-originated mistake, how it was detected, the correction, and the verifying test/commit. Current repository evidence does not independently establish all four parts or the personal discoverer.

One candidate to confirm is the publication-boundary recovery bug: a signal immediately after pointer replacement must not leave a committed snapshot treated as unpublished/interrupted. [The regression](apps/backend/tests/test_refresh_eia.py) (`test_interrupt_immediately_after_pointer_replace_is_published`) injects the interruption after the actual replacement, checks authoritative manifest/version/watermarks, and exercises mirror-write failure and later recovery. [The implementation](apps/backend/src/outage_explorer/refresh_eia.py) rechecks the pointer and repairs audit mirrors from validated publication metadata. This demonstrates the protected boundary, **not evidence that AI originated the earlier bug or that a particular person detected it**. Do not promote the candidate to an authorship claim without confirmation.

## Verification beyond running the app

| Area | Method and representative evidence |
| --- | --- |
| Real source behavior | [Recorded-response tests](apps/backend/tests/test_facility_pagination_evidence.py) recompute key coverage, exact MW sums, and count inconsistencies. [FINDINGS.md](FINDINGS.md) separates verified facts from hypotheses. |
| Ingestion/recovery | [Refresh tests](apps/backend/tests/test_refresh_eia.py) inject pointer-boundary interruption, corrupted audits, schema drift, storage errors, and concurrency; assert unchanged publication/watermarks on pre-commit failure. |
| SQL/RBAC | [SQL tests](apps/backend/tests/test_query.py) reject file readers, dynamic queries, nested references, writes, and multiple statements before data access; test bound literals and result caps. [Auth tests](apps/backend/tests/test_auth.py) cover invalid/expired/tampered credentials and permissions. |
| Demonstration isolation | [Demo tests](apps/backend/tests/test_demo.py) use archive-derived Parquet and real API routers, test personas and safe/unsafe SQL, and verify refresh cannot mutate demo/production state. |
| Reconciliation | [API tests](apps/backend/tests/test_reconciliation_api.py) verify real matching data and synthetic missing keys/dates, micro-MW gaps, invalid inputs, and offsetting facility differences. Synthetic records are not claimed as observed anomalies. |
| Frontend | [Exploration tests](apps/frontend/src/exploration.test.tsx), [reconciliation tests](apps/frontend/src/reconciliation.test.tsx), and [metric tests](apps/frontend/src/fleet-share.test.tsx) exercise mocked HTTP, state cleanup, filtering/pagination, exact formatting, and missing-value chart gaps. |

The code and tests support these verification methods; test presence alone does not identify who designed or executed each check. Commands are in the [README](README.md#running-tests). The [dated setup review](docs/setup-review.md) preserves early environment/dependency results; its original one-test counts and advisory counts are historical.

## Documentation-consolidation checks — 2026-10-22

This session ran 69 focused source-evidence/demo/reconciliation tests, then the full backend suite: **536 passed**, with one existing Starlette TestClient deprecation warning. Frontend `npm test`: **80 passed**; `npm run typecheck` and `npm run build` passed. These commands verify the unchanged application, not personal authorship or browser behavior.

Additional local checks validated 85 relative links/anchors, exactly seven decision entries, preserved ER content, CLI help/subcommands and configured npm scripts, and `git diff --check`. Archive values/counts and temporal examples were recomputed offline; hashes confirmed all 82 previously tracked non-Markdown files unchanged, including evidence, fixtures, application/configuration, and scripts. No live EIA requests, dependency installs, clean-clone setup, or browser session were performed during consolidation.

## Human review and manual browser verification

**User-reported, not automated:** the developer reported manually checking all main application flows in a browser before the final metric-dashboard polish. This report is not a committed browser recording, screenshot suite, or automated end-to-end result. Do not infer verification of the later polish from that earlier statement.

**TODO — developer confirmation:** record browser/date, roles/flows checked, observed outcomes, and whether the latest dashboard polish was subsequently reviewed. Confirm any personally performed source investigation or code review separately; authored commits alone do not establish it.

## Limits and retained evidence

- FastAPI integration tests use ASGI TestClient; frontend tests use jsdom/mock HTTP. Neither equals real browser end-to-end automation or a fresh-clone installation.
- Production ingestion intentionally rejects the unresolved facility total discrepancy. A successful live production publication/refresh is not claimed; the demo's failed refresh is deliberate.
- Three independently verified real anomalies remain missing; matching aggregates do not prove complete source coverage.
- Crash tests inject signals/errors and inspect sync ordering; they do not establish universal hardware/power-loss durability.
- No tracked agent-session/prompt export was found during the documentation audit. If authentic logs are supplied, include only a short sanitized excerpt with context/revision; exclude secrets, private paths, and unrelated material. Do not fabricate a reconstructed transcript.
- Preserve incremental Git history for submission. Optional prompt evidence does not replace the required confirmed AI-mistake explanation.
