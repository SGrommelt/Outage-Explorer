# Engineering Notes

## AI assistance and attribution

Application code was generated primarily with **Codex**; the developer did not manually author application code. Codex also assisted with tests, reviews, and documentation. **ChatGPT** supported planning, technical discussions, and review. The developer directed the work, reviewed outputs and findings, requested corrections, and verified results.

The tracked history through `bbe1678` contains 20 incremental commits, progressing from setup and source exploration through ingestion/recovery, analytics, authentication/RBAC, APIs, frontend, real evidence, isolated demo, reconciliation, and metric polish. Later commits retain documentation and findings work. The repository is not shallow. History and fixtures substantiate an iterative workflow, not a complete prompt transcript.

## Concrete AI-generated mistake and correction

The initial Codex-generated incremental-refresh implementation could classify an interrupted publication as unpublished after the authoritative `current.json` pointer had already been replaced. The developer requested an AI-assisted code review that identified the issue, reviewed the finding, requested a correction, and verified the result.

[The corrected implementation](apps/backend/src/outage_explorer/refresh_eia.py) determines publication from the authoritative pointer and validated snapshot metadata rather than trusting only an in-memory flag. Recovery reconciles audit mirrors with that publication metadata. [The regression](apps/backend/tests/test_refresh_eia.py), `test_interrupt_immediately_after_pointer_replace_is_published`, replaces the actual pointer and then injects an interruption. It checks the manifest, published version, and watermarks, including mirror-write failure and subsequent recovery.

The correction and regression are included in commit `0106423`. **The faulty working-tree version was not separately committed; there is no Git before-and-after diff of that original defect.** AI origin and the review sequence above are developer-confirmed, while the committed code and test substantiate the correction and verification mechanism.

## Verification beyond running the app

| Area | Method and representative evidence |
| --- | --- |
| Real source behavior | [Recorded-response tests](apps/backend/tests/test_facility_pagination_evidence.py) recompute key coverage, exact MW sums, and count inconsistencies. [FINDINGS.md](FINDINGS.md) separates verified facts from hypotheses. |
| Ingestion/recovery | [Refresh tests](apps/backend/tests/test_refresh_eia.py) inject pointer-boundary interruption, corrupted audits, schema drift, storage errors, and concurrency; assert unchanged publication/watermarks on pre-commit failure. |
| SQL/RBAC | [SQL tests](apps/backend/tests/test_query.py) reject file readers, dynamic queries, nested references, writes, and multiple statements before data access; test bound literals and result caps. [Auth tests](apps/backend/tests/test_auth.py) cover invalid/expired/tampered credentials and permissions. |
| Demonstration isolation | [Demo tests](apps/backend/tests/test_demo.py) use archive-derived Parquet and real API routers, test personas and safe/unsafe SQL, and verify refresh cannot mutate demo/production state. |
| Recorded anomaly analysis | [Offline analysis tests](apps/backend/tests/test_analysis_eia.py) reproduce the Facility counts, synchronized capacity step, exact daily outage changes, type-7 quartiles/Tukey fence, and generator contributions from saved evidence. These are one API inconsistency and two unusual observations, not three confirmed source-data errors. |
| Reconciliation | [API tests](apps/backend/tests/test_reconciliation_api.py) verify real matching data and synthetic missing keys/dates, micro-MW gaps, invalid inputs, and offsetting facility differences. Synthetic records are not claimed as observed anomalies. |
| Frontend | [Exploration tests](apps/frontend/src/exploration.test.tsx), [reconciliation tests](apps/frontend/src/reconciliation.test.tsx), and [metric tests](apps/frontend/src/fleet-share.test.tsx) exercise mocked HTTP, state cleanup, filtering/pagination, exact formatting, and missing-value chart gaps. |

The code and tests support these verification methods; test presence alone does not identify who designed or executed each check. Commands are in the [README](README.md#running-tests). The [dated setup review](docs/setup-review.md) preserves early environment/dependency results; its original one-test counts and advisory counts are historical.

## Documentation-consolidation checks — 2026-10-22

The historical documentation-consolidation session ran 69 focused source-evidence/demo/reconciliation tests, then the full backend suite: **536 passed**, with one existing Starlette TestClient deprecation warning. Frontend `npm test`: **80 passed**; `npm run typecheck` and `npm run build` passed. These commands verify the unchanged application, not personal authorship or browser behavior.

Additional local checks validated 85 relative links/anchors, exactly seven decision entries, preserved ER content, CLI help/subcommands and configured npm scripts, and `git diff --check`. Archive values/counts and temporal examples were recomputed offline; hashes confirmed all 82 previously tracked non-Markdown files unchanged, including evidence, fixtures, application/configuration, and scripts. No live EIA requests, dependency installs, clean-clone setup, or browser session were performed during consolidation.

## Human review and manual browser verification

**Developer-confirmed, not automated:** the developer personally tested the application in a browser, including the latest changes, and reviewed outputs and correction results. No specific browser, date, or scenario inventory is claimed here. This verification is distinct from FastAPI integration tests and jsdom component tests; no committed browser recording or automated browser end-to-end suite is claimed.

## Limits and retained evidence

- FastAPI integration tests use ASGI TestClient; frontend tests use jsdom/mock HTTP. Neither equals real browser end-to-end automation or a fresh-clone installation.
- Production ingestion intentionally rejects the unresolved facility total discrepancy. A successful live production publication/refresh is not claimed; the demo's failed refresh is deliberate.
- Three reproducible findings are documented: one API inconsistency and two unusual valid observations. No source-data defects are established; matching aggregates do not prove source completeness.
- Crash tests inject signals/errors and inspect sync ordering; they do not establish universal hardware/power-loss durability.
- No tracked agent-session/prompt export was found during the documentation audit. If authentic logs are supplied, include only a short sanitized excerpt with context/revision; exclude secrets, private paths, and unrelated material. Do not fabricate a reconstructed transcript.
- Preserve incremental Git history for submission. Optional prompt evidence supplements the confirmed AI-mistake explanation above.
