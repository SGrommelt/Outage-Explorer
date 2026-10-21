# EIA facility pagination investigation

Initial investigation on 2026-10-21 covered **2026-10-20 only**. A bounded follow-up on the same day covered **2026-10-18–19**; its evidence and current conclusion are below. Requests are now stopped. No extraction acceptance rule or publication/recovery code was changed.

## Verified result

The mismatch is independently reproducible in HTTP responses, outside the extractor and its Parquet writer:

- A Python standard-library `urllib` request with `length=100`, `offset=0` returned HTTP 200, **55 facility rows**, and `response.total="95"`.
- The same query at **offset 55** returned HTTP 200, **an empty data array**, and `response.total="95"`.
- Those 55 rows have 55 distinct `(period, facility)` keys.

The exact two responses, metadata, and credential-free query parameters are retained in [the recorded fixture](../apps/backend/tests/fixtures/eia_facility_pagination_2026-10-20.json). Neither missing rows nor corrected totals were synthesized in this fixture.

This establishes an **EIA API response/count inconsistency**, not a client-side loss of 40 records. It does not establish whether the total is wrong or the source omits rows, and does not independently prove complete historical source coverage.

## Exact request review

Base URL: `https://api.eia.gov/v2/nuclear-outages/`. Facility path: `facility-nuclear-outages/data/`.

The extraction query uses these parameters; the API key is intentionally excluded:

```json
{
  "frequency": "daily",
  "start": "2026-10-20",
  "end": "2026-10-20",
  "length": 25,
  "offset": 0,
  "data[0]": "capacity",
  "data[1]": "outage",
  "data[2]": "percentOutage",
  "sort[0][column]": "period",
  "sort[0][direction]": "asc",
  "sort[1][column]": "facility",
  "sort[1][direction]": "asc"
}
```

The original extraction advanced offsets by actual returned counts: **0, 25, 50, 55**, with row counts **25, 25, 5, 0**. No entity facets were applied to this baseline. EIA's [pagination documentation](https://www.eia.gov/opendata/documentation.php) defines offset as a row position and total as all rows responsive to the query; it does not require offsets to be multiples of length.

For successful diagnostic responses, the server's echoed parameters matched the requested frequency, date bounds, length, offset, measures, sorting, and optional facility filter. Authentication parameters were removed from retained echoes. Metadata continued to advertise daily `YYYY-MM-DD` data and the expected identifier facets and measures. Recorded data responses reported `apiVersion="2.1.14"`, `frequency="daily"`, and `dateFormat="YYYY-MM-DD"`.

## Bounded comparisons

The [sanitized comparison evidence](evidence/eia-pagination-2026-10-20.json) contains exact parameters, response metadata/echoes, counts, identifiers, and failures. Initial partial cases are retained as partial; successful targeted follow-ups are separate entries. The `locally_verified_comparisons` section combines their observed counts/key sets, without treating timeouts as empty responses.

| Query / control | Observed returned rows | Reported total | Terminal response / comparison |
| --- | --- | --- | --- |
| Facility, length 100, ascending | 55 | 95 | Empty at offset 55; independent two-request recording |
| Facility, length 10, ascending | 10, 10, 10, 10, 10, 5 | 95 throughout | Empty at offset 55; same 55 facility IDs |
| Facility, length 25, descending | 25, 25, 5 | 95 throughout | Empty at offset 55; same 55 facility IDs |
| Facility, length 25, ascending | Original extraction: 25, 25, 5 | 95 throughout | Original extraction ended empty at 55; investigation repeated the first 50 rows and empty 55, but its offset-50 requests timed out |
| Facility, capacity only, length 100 | 55 | 95 | Empty at offset 55; selecting fewer measures did not fix the count |
| Facility boundary probes, length 25 | 0 at offsets 75 and 95 | 95 | No rows appeared at conventional page-boundary offsets either |
| National, length 25 | 1 | 1 | Empty at offset 1; no count discrepancy observed |
| Generator, length 100 | 95 unique generator keys | 95 | Empty at offset 95; no count discrepancy observed |
| Facility filtered to `facility=46` | 1 | 3 | Empty at offset 1 |
| Generator filtered to `facility=46` | 3 | 3 | Empty at offset 3 |

The smaller-page and descending facility comparisons were completed using saved prefixes plus successful targeted follow-ups. Their combined counts and distinct facility IDs both equal 55 and match the independent single-page response. The initial generator length-25 diagnostic stopped after 75 rows because of a timeout; the separate length-100 control, not that partial run, verifies 95/95 and an empty terminal page.

The complete 95-row generator control spans **55 distinct facilities**, exactly matching the facility route's returned IDs. Facility 46 has three generator rows but one facility row, while the facility route reports a total of three even with that filter.

## Interpretation: facts versus hypotheses

**Verified:** the request parameters are interpreted as intended, the independent transport reproduces the mismatch, reducing page size and reversing sort order do not resolve it, and national/generator controls match their advertised counts for this date. There is no evidence that the extractor skipped rows, applied an unintended facet, truncated a page, or lost records during conversion/writing.

**Supported hypothesis, not confirmed:** the facility route may count generator-level rows before aggregating them into facility rows. The 95 generator rows / 55 facilities relationship and the filtered 3-generators / 1-facility control support this explanation. Server code, database queries, or confirmation from EIA were not obtained, so the internal mechanism remains unproven.

**Unresolved:** whether the 55 facility rows constitute the complete authoritative source dataset, why EIA's total differs, whether other dates have the same behavior, and whether revisions affect results. These observations do not justify accepting a smaller count, inventing 40 rows, or treating this as a resolved source-data issue.

Connection reliability is a separate unresolved limitation. HTTPX attempts and some standard-library requests timed out while other requests succeeded. An isolated authenticated metadata trace returned 200 promptly after TCP/TLS setup. The completed independent comparison ledger records 28 initial requests plus 15 targeted follow-up attempts, including nine timeouts. A separate two-request independent confirmation supplies the regression fixture. Earlier HTTPX diagnostics also experienced timeouts; an interrupted attempt has no complete final ledger. No timeout was counted as an empty page or as evidence of completeness. Requests were capped per diagnostic run and fixed to this one day; no historical dataset was downloaded. No further live checks were required for that initial conclusion; the explicitly requested follow-up below tests the remaining multi-date/count-grain hypothesis.

## Safety and regression

Strict completeness validation remains intact: an empty page before the advertised total rejects the entire snapshot. All three routes must validate before the current snapshot pointer can change. There is no total override or bypass option.

`test_recorded_oct20_facility_total_95_with_55_rows_preserves_snapshot` replays the actual independent `length=100` responses at offsets 0 and 55. It verifies the unchanged `total="95"`, 55 unique returned keys, the expected extraction failure, byte-for-byte preservation of an existing same-window snapshot/pointer, and staging cleanup. Only the prior-snapshot setup uses consistent synthetic responses, explicitly labeled in the test; the failing responses are recorded evidence.

Run offline from the repository root:

```sh
apps/backend/.venv/bin/python -m pytest apps/backend/tests
```

The strict implementation and regression are suitable to commit after offline checks pass. Publication of the affected real three-route snapshot remains blocked until the discrepancy or an independently verified completeness policy is resolved. No commit has been created.


## Targeted follow-up: October 18–19, 2026

### Scope and retained evidence

The [follow-up recording](evidence/eia-pagination-2026-10-18_19.json) retains credential-free parameters, exact response bodies, API versions, and computed comparisons. Its UTC timestamp is recorded in `recorded_at_utc`. Unlike a published snapshot, this is diagnostic evidence only. Nothing was written to the extraction or incremental publication roots.

Before networking, the implementation, existing pagination regressions, actual wire schemas, incremental merge/watermark rules, pointer commit/recovery logic, and this document were reviewed. No implementation-side offset, filter, column-selection, or conversion loss was found. In particular, offsets advance by actual received rows and the original count is never replaced with a convenient unique-key count.

A network-sandbox preflight made four connection attempts with **no HTTP responses** (`ConnectError`). After network permission, **exactly nine authenticated EIA requests** completed with HTTP 200, no warnings, and API version `2.1.14`. The run used HTTPX with a fresh connection per request, a 15-second timeout, no retries, and a hard nine-request budget. There were no timeouts in this successful follow-up; earlier intermittent timeouts remain an unresolved operational limitation. No additional EIA requests are needed to reproduce the offline conclusions.

All data queries used `frequency=daily`, `start=2026-10-18`, `end=2026-10-19`, `length=200`, all three explicitly advertised measures (`capacity`, `outage`, `percentOutage`), and ascending `period` followed by route identifiers. Facet probes used the documented `facets[facility][]=...` syntax. Full parameters are in the recording; credentials and request echoes are excluded.

| Request | Route / parameters beyond that baseline | Returned rows | Reported total |
| --- | --- | --- | --- |
| 1 | Facility metadata (no data query) | Not applicable | Not applicable |
| 2 | Facility, offset 0 | 110 | 190 |
| 3 | Facility, offset 110 | 0 | 190 |
| 4 | Generator, offset 0 | 190 | 190 |
| 5 | Generator, offset 190 | 0 | 190 |
| 6 | National, offset 0 | 2 | 2 |
| 7 | National, offset 2 | 0 | 2 |
| 8 | Facility 1715 (one generator per observed day), offset 0 | 2 | 2 |
| 9 | Facility 3265 (three generators per observed day), offset 0 | 2 | 6 |

The two filtered probes are single bounded requests, not terminal-page enumerations. They establish returned-versus-reported counts for those filters, without claiming a separately complete filtered traversal.

### Verified facts

- Each of October 18 and 19 has **55 unique facility keys, 95 unique generator keys, and one national row**. All returned records pass the existing required-field, date, units, decimal precision, and natural-key validation. Metadata still advertises facility grain, only the `facility` facet, daily frequency, and the same three measures; it does not expose a `generator` dimension on the facility route. The metadata end boundary had advanced to October 21; those later records were not requested.
- The facility `(period, facility)` key set equals the projection of the complete **190/190 returned generator records**. There are no unmatched keys in either direction. This accounts for all facilities represented by the generator response for these dates; it does **not** prove an independent, authoritative fleet inventory.
- Every facility's capacity and outage MW exactly equals the Decimal sum of its corresponding generators on the same date. No tolerance or rounding was needed for those measures. Each facility's returned percentage also equals its outage/capacity percentage rounded half-even to two decimal places in this sample. Percentages were not summed.
- National capacity is `100056.7` MW on each date. National outage is `19113.734` MW on October 18 and `19896.304` MW on October 19. Facility capacity/outage sums match those values exactly on both days. The comparison preserves compatible dates and aggregation levels; it never combines national and lower-grain values into a new total.
- The terminal facility response is still empty before its advertised total, while generator/national terminal responses agree with their advertised totals. Together with the earlier October 20 evidence, the discrepancy spans **three observed dates**, and a two-day query as well as a one-day query. Nothing here establishes a universal historical pattern.
- Prior smaller-page, reverse-sort, capacity-only, boundary-offset, and multi-generator-facet controls remain relevant. Explicit projection, documented facet filters, and deterministic sorting do not provide a demonstrated pagination repair.

### Documentation review and hypotheses

The reviewed [EIA API technical guide](https://www.eia.gov/opendata/documentation.php) describes projection through `data[]`, advertised facets, frequency/date filters, sorting, and row-based length/offset. Its pagination section defines `total` as the number of responsive rows, independent of the requested page. It provides **no facility-route exception, count-before-grouping contract, documented grouping/deduplication switch, or alternate authoritative facility count**. The March 2026 patch notes mention an earlier date/count bug; that is not evidence that this separate behavior is intended or corrected.

**Strongly supported hypothesis:** the facility route's count query operates on generator-grain rows before a facility-grain grouping/aggregation. The full-window and one-/three-generator facet controls are consistent with that hypothesis. Matching MW sums show an aggregate relationship in the returned data; neither observation reveals EIA's server query implementation.

**Competing unresolved explanations:** the reported count could describe a broader or differently filtered source population than the returned aggregation; shared omissions or source filters could affect both facility and generator results; or a source/API defect could make counts or coverage unreliable. Cross-route agreement is an internal consistency check on responses that share an upstream source, not independent evidence that an entity was never omitted. National MW equality alone cannot detect every possible omission, such as zero-valued records.

### Resolution decision and exact blocker

**No safe ingestion correction has been established.** The facility response simultaneously advertises 190 responsive rows, supplies 110 unique rows, and returns an empty array at offset 110. The evidence does not authoritatively resolve what the other 80 counted items represent. Treating them as irrelevant generator counts would replace a source contract with our hypothesis. Therefore neither substituting 110 for 190, accepting the first empty/short page, synthesizing extra facility rows, nor deriving replacement facility data from generators is implemented.

Real three-route publication remains **blocked and not yet safe** under the current completeness contract. The new checks establish coherent returned data, not permission to publish it. Existing valid snapshots and watermarks were not touched; no attempt to publish a new real snapshot was made because no correction passed this evidence gate.

The smallest next step is **authoritative clarification from EIA**, using the credential-free request pair at offsets 0/110 plus the facility-3265 2-versus-6 control: does this route intentionally count underlying generator records, and does it guarantee one returned record per `(period, facility)` represented in the generator route? Request a corrected row count or a documented way to obtain it. This does not require another download or broad date sweep. If EIA cannot confirm that contract, obtain an independently authoritative facility inventory for a single affected date before designing a different completeness policy. Confirmation would inform a separately reviewed per-date validation policy; it would not justify a general total override or a fixed 55-facility assumption.

### Offline regression and safety

`apps/backend/tests/test_facility_pagination_evidence.py` recomputes natural-key uniqueness, exact key-set equality, per-facility generator MW sums, national MW sums, percentage rounding, and facet counts directly from the new response bodies. Separate window and incremental replays preserve the original 190 total and empty page at offset 110, and both reject publication despite those cross-route matches. The incremental replay verifies a durable failed audit, fetched row counts of 2/110/190, no published version, and no advanced watermarks. Staging is cleaned in both workflows.

The existing October 20 replays continue to verify byte-for-byte preservation of an existing publication and unchanged watermarks. Existing interruption/recovery, schema, duplicate-key, conflict, and atomic-publication regressions remain unchanged. No production extraction, refresh, pointer, watermark, or crash-recovery code was modified. These tests use recordings and temporary directories only.


## Isolated 30-day diagnostic analysis

A subsequent explicitly isolated analysis collected September 22–October 21, 2026: 30 national, 1,650 facility, and 2,850 generator rows over 30 dates. Facility responses still reported 2,850 and ended empty at offset 1,650. This is retained diagnostic evidence only, not a correction of count semantics or a published snapshot. All daily and facility-day MW comparisons matched exactly, which remains internal consistency evidence rather than authoritative coverage. The [README reconciliation report](../README.md#real-data-reconciliation-evidence-us-07) gives provenance, the bounded request/failure ledger, compact source archive, offline replay commands, and the explicit shortfall of zero observed source-data anomalies. Production acceptance, pointers, audits, and watermarks remain unchanged.


The later [offline temporal investigation](../README.md#offline-temporal-investigation-and-anomaly-shortfall) made no additional API requests. It rules out membership change in the saved population, attributes the October 1 fleet step numerically to stable-ID capacity changes, and finds no percentage/attribute inconsistency beyond normal two-decimal rounding. It does not establish capacity methodology or resolve the pagination count. One API problem is not counted repeatedly to meet the three-anomaly requirement; the explicit shortfall remains. The [implemented model](DATA_MODEL.md) documents actual keys, types, optional logical relationships, publication/audit semantics, and seven decisions.
