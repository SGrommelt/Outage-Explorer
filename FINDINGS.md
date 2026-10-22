# Real-data findings

## Scope and provenance

The evidence covers **September 22–October 21, 2026 inclusive: 30 consecutive days** from the EIA national, facility, and generator nuclear-outage routes. It is **non-production evidence with unverified source completeness**, not an accepted production snapshot.

- [Sanitized source archive](docs/evidence/eia-real-30-days-source.json.gz): exact retained response bodies, credential-free query parameters, metadata, API version `2.1.14`, per-response checksums, timestamps, and failure history; approximately 61 KB compressed.
- [Reconciliation summary](docs/evidence/eia-real-30-days-summary.json): archive SHA-256, coverage/counts, daily comparisons, representative facility comparisons, and the historical anomaly assessment.
- [Temporal summary](docs/evidence/eia-real-30-days-temporal-summary.json): capacity decomposition, attribute/percentage checks, and the historical candidate classification. These pinned summaries predate the broader anomaly interpretation; regenerate current analysis with the commands below rather than treating their requirement-status fields as current.

Collection began `2026-10-21T20:43:21.751480Z`; targeted resume began `2026-10-21T20:46:11.439075Z`. The recorded collection/resume used **14 network attempts: 12 HTTP 200 responses and two failed network attempts**. A separate sandbox preflight exhausted six attempts without HTTP responses. The generator continuation failed after 2,000 rows, then resume retrieved the remaining 850 and terminal page. Network failure is not an empty source page or a source-data anomaly.

Collection used sequential 1,000-row pages, ascending period/identifier ordering, at most eight pages per route, two attempts per request, a quarter-second interval, and a 36-attempt budget shared across resumptions. Offset pagination does not provide source snapshot isolation. Checksums detect accidental changes; they are not EIA signatures or independent provenance certification. Earlier independent single-/two-day controls are preserved in the [dated investigation](docs/EIA_PAGINATION_INVESTIGATION.md).

## Coverage and pagination

| Route | Returned / validated unique rows | Dates present | Reported total | Page row counts, including terminal page |
| --- | ---: | ---: | ---: | --- |
| `us-nuclear-outages` | 30 / 30 | 30 | 30 | 30, 0 |
| `facility-nuclear-outages` | 1,650 / 1,650 | 30 | 2,850 | 1,000, 650, 0 |
| `generator-nuclear-outages` | 2,850 / 2,850 | 30 | 2,850 | 1,000, 1,000, 850, 0 |

Every returned date has one national row, 55 facilities, and 95 generators. Received records pass required-field, units, date-window, exact decimal precision, and provisional natural-key checks. No duplicate keys were observed. These checks describe the received population, not an independent fleet inventory.

## Reconciliation method and verified matching results

Offline replay reuses `schema_for`/`validate_record`, analytical `VIEWS`, and the existing `reconcile_daily`, `reconcile_facilities`, and `find_anomalies` functions in an isolated in-memory DuckDB connection. Generators are aggregated to `(date, facility)` before comparison with facility observations. Each grain is independently aggregated by date for national comparisons. Parent/child rows are never joined and then summed in a way that multiplies parent values. Percentages are not summed; all MW gaps use exact decimal arithmetic, with no tolerance needed for this evidence.

**All 30 daily comparisons and all 1,650 facility-day comparisons match exactly for capacity and outage MW.** There are no unmatched facility/generator groups. The six daily pairwise gaps (facility−national, generator−national, generator−facility, for both measures) are all `0.000000 MW`.

Each pair below is **(capacity MW, outage MW)**:

| Date | National reported | Facility sum | Generator sum | All daily pairwise gaps MW |
| --- | --- | --- | --- | --- |
| 2026-09-22 | (97620.500000, 7745.638000) | (97620.500000, 7745.638000) | (97620.500000, 7745.638000) | 0.000000 |
| 2026-10-06 | (100056.700000, 15268.765000) | (100056.700000, 15268.765000) | (100056.700000, 15268.765000) | 0.000000 |
| 2026-10-21 | (100056.700000, 19936.436000) | (100056.700000, 19936.436000) | (100056.700000, 19936.436000) | 0.000000 |

| Date | Facility ID | Generator rows | Facility capacity = generator sum MW | Facility outage = generator sum MW | Generator−facility capacity / outage gap MW |
| --- | --- | ---: | ---: | ---: | --- |
| 2026-09-22 | 46 | 3 | 3661.700000 | 1227.400000 | 0.000000 / 0.000000 |
| 2026-10-06 | 6008 | 3 | 4003.000000 | 1333.000000 | 0.000000 / 0.000000 |
| 2026-10-21 | 3166 | 2 | 2694.100000 | 1346.700000 | 0.000000 / 0.000000 |

These are matching examples, **not anomalies**. The results demonstrate internal consistency under these comparisons. Shared omissions, definitions, or revisions can still exist. No actual nonzero cross-grain MW discrepancy was found to explain away.

## Finding 1 — Facility API count inconsistency

**Classification: verified API/metadata inconsistency. Anomaly does not necessarily mean erroneous data.** Only this count issue is a verified inconsistency; findings 2 and 3 are internally consistent observations, not confirmed source defects.

The facility route reports **2,850** records, returns **1,650** unique facility-day rows, then returns an empty array at offset **1,650**, while still reporting 2,850. The difference is **1,200 counted items**, whose meaning is unresolved; it is not evidence that 1,200 identifiable facility rows are missing.

Independent earlier controls reproduced 55/95 and an empty offset-55 page on October 20; October 18–19 returned 110/190 with an empty offset-110 page. A filtered three-generator facility (`3265`) returned two facility rows for two days but reported six, while a one-generator facility (`1715`) returned/reported two. Smaller pages, reversed ordering, and capacity-only projection did not repair the discrepancy. Exact recordings and request scope are in the [investigation](docs/EIA_PAGINATION_INVESTIGATION.md#targeted-follow-up-october-1819-2026).

**Hypothesis, not an established root cause:** EIA may count underlying generator rows before facility aggregation. The reviewed [API guide](https://www.eia.gov/opendata/documentation.php) supplies no confirmed facility-specific exception establishing that interpretation. Alternative explanations include different source populations/filters or API count/coverage defects. Related-grain equality cannot distinguish all these possibilities.

**Handling:** production extraction rejects the early empty page. No partial snapshot is published and failed pre-publication refreshes do not advance watermarks. The isolated archive/demo retains both reported and actual counts and the explicit caveat. Resolving production acceptance requires authoritative count semantics/corrected totals or an independently justified completeness source, not a hardcoded facility count or silent total override.

Historical validation on October 21 for October 20 used page size 25, a 20-second timeout, and up to three attempts: national 1/1, facility 55/95 (25, 25, 5, then empty), generator 95/95. All 151 received records passed individual validation; the diagnostic finished in approximately 4.8 seconds without retries. It exited 1, removed staging, and published nothing. The [recorded regression](apps/backend/tests/test_extract_eia.py) checks preservation of a prior valid same-window publication. This is a historical run outcome, not a statement about files currently on a developer machine.

This is **one API metadata/pagination anomaly**, not one anomaly per date, facet, or missing counted item.

## Finding 2 — Synchronized capacity reporting change

**Classification: unusual structural/reporting observation, not a confirmed source defect.**

National capacity rises from **97620.5 MW** to **100056.7 MW**, a **2436.2 MW** step (**approximately 2.495582%**). The received population contains the same 55 facility IDs and 95 `(facility, generator)` identities on every day. All capacity changes occur on October 1: **47/55 facilities (85.454545%) and 78/95 generators (82.105263%)** increase; eight facilities and 17 generators are unchanged. At each lower grain, stable-key changes sum exactly to 2436.2 MW; added/removed capacity contributions are zero.

| Facility | Generator | 2026-09-30 capacity MW | 2026-10-01 capacity MW | Change MW |
| --- | --- | ---: | ---: | ---: |
| 46 | 1 | 1227.4 | 1254.8 | +27.4 |
| 3166 | 2 | 1264.7 | 1346.7 | +82.0 |
| 7722 | 1 | 1123 | 1179 | +56 |

**Verified:** existing identifiers account numerically for the full step; observed membership change does not. **Unconfirmed:** a coordinated rating refresh or seasonal capacity basis could explain it. EIA's [capacity FAQ](https://www.eia.gov/tools/faqs/faq.php?id=101) distinguishes summer/winter ratings, while the [nuclear data index](https://www.eia.gov/nuclear/data.php) labels the outage product as using annual capacity. Neither reviewed reference nor retained metadata establishes an October 1 rule. One collected vintage cannot prove revisions to previously published values. No verified invariant requires capacity to remain constant, so this is not a confirmed defect or three separate anomalies.

The product preserves each day's capacity and uses that day's national denominator for fleet offline share. Valid capacity changes alone do not fail ingestion. The offline inspector inventories the changes; there is no specialized automatic UI warning.

**Why unusual:** all 78 generator changes occur on this boundary; the other 28 daily transitions have none. Capacities are stable within September 22–30 and October 1–21. Among affected generators, increases range from 1 to 82 MW (linear-interpolated Q1 22 MW, median 29.85 MW, Q3 37.8 MW). This synchronization is exceptional within the received window, without establishing long-term rarity.

## Finding 3 — October 3 fleet-wide outage increase

**Classification: exploratory operational outlier, not a source-data error.** Between October 2 and October 3, 2026, national outage increases from **12295.214 to 16107.683 MW**, a **3812.469 MW** increase. Capacity remains **100056.7 MW**; fleet offline share rises approximately **3.810309 percentage points**.

The comparison population is all **29 adjacent daily national absolute outage changes** in the archive. Quartiles use **type-7 linear interpolation at index `(n−1)×p`** in the sorted sample. Q1 is **48.872 MW**, Q3 **1047.235 MW**, and IQR **998.363 MW**. The conventional exploratory upper fence `Q3 + 1.5×IQR` is **2544.7795 MW**. October 3 is the **only** absolute daily change strictly above this fence; the second largest is 2483.251 MW. The fence is computed from the archive, not a configured production threshold.

| Facility / generator | October 2 outage MW | October 3 outage MW | Change MW |
| --- | ---: | ---: | ---: |
| Palo Verde 6008 / 1 | 0 | 1333 | +1333 |
| Sequoyah 6152 / 2 | 46.204 | 1155.1 | +1108.896 |
| H B Robinson 3251 / 2 | 71.361 | 792.9 | +721.539 |
| Prairie Island 1925 / 1 | 10.92 | 546 | +535.08 |
| Dresden 869 / 3 | 9.412 | 18.824 | +9.412 |
| Quad Cities 880 / 2 | 170.01 | 160.565 | −9.445 |
| Point Beach 4046 / 1 | 29.885 | 35.862 | +5.977 |
| Perry 6020 / 1 | 50.72 | 202.88 | +152.16 |
| Edwin I Hatch 6051 / 2 | 44.15 | 0 | −44.15 |
| **Total change** | | | **+3812.469** |

Generator membership is unchanged and contributions sum exactly to the national increase. Capacity is unchanged on these dates, distinguishing this event from finding 2. Legitimate operating/maintenance changes can produce such a surge; no common cause or reporting error is established.

**Handling:** the national chart/table, authorized SQL, and reconciliation expose the observations. Reconciliation correctly reports matching aggregates. The offline inspector flags this exploratory outlier; the application has no specialized automatic warning. Existing large-change diagnostics remain opt-in with a caller-supplied threshold.

**Statistical limitations:** 30 days provide only 29 transitions, with dependence and seasonal effects. This is an in-window descriptive screen, not a significance test or long-term baseline. Signed-change screening is a different question and also flags October 12 and October 15; the absolute-change population is stated explicitly rather than presenting the result as method-independent.

## Other checks and weaker candidates

There are zero membership changes, temporal label changes, missing labels, or facility/generator label disagreements in the archive. Existing diagnostics find zero missing route dates, invalid required records, duplicate keys, negative capacity/outage, outage-above-capacity, or percentages outside 0–100 across the **4,530** observations.

Source percentage strings have at most two decimal places: national counts are 28 two-place and two one-place; facility counts are 305 two-place, 52 one-place, and 1,293 whole numbers; all 2,850 generator values are whole numbers. The inspector tests a nearest-two-decimal **0.005 percentage-point half-unit interval** using exact cross multiplication and 60-digit working precision. This is an explicit comparison assumption, not a verified EIA rounding/formula contract. No observations exceed it; equality at the boundary is not flagged. Nonpositive capacity is inventoried as an undefined ratio, not divided or zero-filled.

Representative maximum absolute percentage gaps (calculated ratios/gaps below are approximate display values):

| Date / scope | Capacity MW | Outage MW | Reported % | Calculated % | Reported−calculated percentage points |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-10-19 / national | 100056.7 | 19896.304 | 19.89 | 19.885029188 | +0.004970812 |
| 2026-09-27 / facility 6026 | 2264.4 | 226.1 | 9.98 | 9.984984985 | −0.004984985 |
| 2026-09-22 / facility 46, generator 1 | 1227.4 | 1227.4 | 100 | 100 | 0 |

Ordinary rounding is not counted as an anomaly. Automatic large-change flags require a justified caller-supplied threshold; normal outage starts/ends are not automatically bad data. Range flags are review candidates, not claims about universal EIA constraints. Synthetic mismatch/invalid fixtures prove detection behavior, not real anomaly evidence.

## Interpretation and product boundaries

The three primary findings address distinct delivery, capacity-reporting, and outage-behavior categories. They reasonably address the challenge's broad anomaly wording; they do **not** establish three source-data defects. Zero confirmed source-data defects were found. Matching aggregates do not certify source completeness; **Facility source completeness remains unverified and production publication remains blocked**.

Clinton 204/1 (October 19–21: 0 → 11.071 → 0 MW) and Vogtle 649/3 (October 11–13: 0 → 77.98 → 0 MW) are not additional primary anomalies. There are 22 exact three-day reversals across 15 generators. Palo Verde's full-outage transition contributes to finding 3 and is not counted separately. Ordinary rounding and synthetic fixtures are not real anomalies.

The read-only demo exposes these received observations without certifying source completeness. Analyst/Admin reconciliation reports exact gaps and observed missing keys with evidence-based reasons, never invented causes. Missing/invalid comparisons are unavailable; zero-valued orphan groups and offsetting facility gaps are detected by synthetic regressions. Viewer can access only national data/metric, not reconciliation or lower grains. Demo refresh explicitly fails without EIA calls or production state changes. [Demo startup](README.md#quick-start--working-real-data-demo) and [model/validation](docs/DATA_MODEL.md) describe these boundaries.

## Offline reproduction

Run from the repository root with backend dependencies installed. No API key or live request is needed; generated outputs stay in Git-ignored `data/eia-analysis` and never update production pointers/audits/watermarks:

```sh
apps/backend/.venv/bin/python -m outage_explorer.analysis_eia analyse \
  --input docs/evidence/eia-real-30-days-source.json.gz \
  --output data/eia-analysis/reproduced/analysis.json
apps/backend/.venv/bin/python -m outage_explorer.analysis_eia inspect \
  --input docs/evidence/eia-real-30-days-source.json.gz \
  --output data/eia-analysis/temporal-inspection.json
apps/backend/.venv/bin/python -m pytest \
  apps/backend/tests/test_analysis_eia.py \
  apps/backend/tests/test_facility_pagination_evidence.py \
  apps/backend/tests/test_reconciliation_api.py
```

**Reproduce each finding with the `inspect` command above:** finding 1 is in `route_counts` (also `analyse.routes`), finding 2 in `capacity_changes` and `capacity_decomposition`, and finding 3 in `national_daily_outage_changes` and `outage_change_screening`. All use the same saved source archive linked above; no API key is loaded. Generator contributions are included for every adjacent national transition. Empty samples have null quartiles/fence; constant samples have no strictly-above-fence observations.

The first output includes all daily/facility comparisons and original counts; the second additionally includes stable-ID changes, ratio candidates, and attributes. The saved summaries provide compact pinned examples. The [source-schema observations](docs/EIA_SCHEMA.md) and [historical investigation](docs/EIA_PAGINATION_INVESTIGATION.md) retain metadata, controls, source links, and request/failure ledgers. No new collection is needed to reproduce these findings.
