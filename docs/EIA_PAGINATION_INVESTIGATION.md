# EIA facility pagination investigation — conclusion

Investigated on 2026-10-21 for **2026-10-20 only**. Live requests have stopped. No extraction acceptance rule or atomic publication code was changed.

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

Connection reliability is a separate unresolved limitation. HTTPX attempts and some standard-library requests timed out while other requests succeeded. An isolated authenticated metadata trace returned 200 promptly after TCP/TLS setup. The completed independent comparison ledger records 28 initial requests plus 15 targeted follow-up attempts, including nine timeouts. A separate two-request independent confirmation supplies the regression fixture. Earlier HTTPX diagnostics also experienced timeouts; an interrupted attempt has no complete final ledger. No timeout was counted as an empty page or as evidence of completeness. Requests were capped per diagnostic run and fixed to this one day; no historical dataset was downloaded. No further live checks are required for this conclusion.

## Safety and regression

Strict completeness validation remains intact: an empty page before the advertised total rejects the entire snapshot. All three routes must validate before the current snapshot pointer can change. There is no total override or bypass option.

`test_recorded_oct20_facility_total_95_with_55_rows_preserves_snapshot` replays the actual independent `length=100` responses at offsets 0 and 55. It verifies the unchanged `total="95"`, 55 unique returned keys, the expected extraction failure, byte-for-byte preservation of an existing same-window snapshot/pointer, and staging cleanup. Only the prior-snapshot setup uses consistent synthetic responses, explicitly labeled in the test; the failing responses are recorded evidence.

Run offline from the repository root:

```sh
apps/backend/.venv/bin/python -m pytest apps/backend/tests
```

The strict implementation and regression are suitable to commit after offline checks pass. Publication of the affected real three-route snapshot remains blocked until the discrepancy or an independently verified completeness policy is resolved. No commit has been created.
