# EIA nuclear outage schema exploration

Verified on **2026-10-21**, using real authenticated API responses. The retained sample was fetched at **2026-10-21T06:09:00Z**; EIA reported **apiVersion `2.1.14`**. Findings below are observations of this bounded sample, not a contractual schema or a full-history validation.

## Evidence and reproduction

- [EIA API technical documentation](https://www.eia.gov/opendata/documentation.php): metadata discovery, `data[]` selection, facets, sorting, pagination, and string-valued data points.
- [EIA nuclear outage API browser](https://www.eia.gov/opendata/browser/nuclear-outages): dataset context. Live metadata attributes these routes to the NRC Power Reactor Status Report.
- Recorded evidence: [sanitized response fixture](../apps/backend/tests/fixtures/eia_nuclear_sample.json), containing metadata and **18 real rows**. Request echoes and credentials are excluded. Offline tests replay these exact responses.
- Script: [explore_eia.py](../apps/backend/src/outage_explorer/explore_eia.py). This is a manual schema probe, with no database writes, production extraction, retries, or full-dataset downloads.

From the repository root, after installing the backend's dependencies:

```sh
apps/backend/.venv/bin/python -m outage_explorer.explore_eia --output data/eia-exploration.json
apps/backend/.venv/bin/python -m pytest apps/backend/tests
```

`python-dotenv` loads `apps/backend/.env` without interpolation or overriding an exported `EIA_API_KEY`. Missing/blank credentials fail before any HTTP request. The key is never printed. HTTP request logging is disabled by the CLI because EIA requires the key in a URL query parameter. The script omits request echoes and redacts the key from retained output. `.env` and local `data/` reports are ignored by Git.

## Routes and scope

Base URL: `https://api.eia.gov/v2/nuclear-outages/`. Append each route below with `/` for metadata and `/data/` for rows.

| Level | Verified route | Metadata facets | Proposed natural key, within the daily route |
| --- | --- | --- | --- |
| National | `us-nuclear-outages` | None | `period` |
| Facility | `facility-nuclear-outages` | `facility` — Plant code | `(period, facility)` |
| Generator | `generator-nuclear-outages` | `facility` — Plant code; `generator` — Facility's generator ID number | `(period, facility, generator)` |

These keys were complete and unique across the six sampled rows per route. They remain **provisional until checked against full historical data**, including missing identifiers, duplicates, and identifier reuse. They are proposals based on the advertised grain and observed rows, not proven historical constraints. The generator sample repeats generator `"1"` across facilities `"46"`, `"204"`, and `"210"`; a generator ID is not globally unique. Facility names are descriptive labels and are not keys. Retain route context if combining levels.

## Verified metadata

All three metadata responses contained:

- `id`, `name`, `description`: strings describing the route.
- `frequency`: a list containing daily frequency (`id: "daily"`, `query: "D"`, `format: "YYYY-MM-DD"`) and its description.
- `facets`: lists of identifier definitions as shown above; empty for the national route.
- `data`: an object advertising **exactly three selectable measures** in these responses: `capacity`, `outage`, and `percentOutage`. Each definition contains `units`.
- `startPeriod: "2007-01-01"`, `endPeriod: "2026-10-20"`.
- `defaultFrequency: "daily"`, `defaultDateFormat: "YYYY-MM-DD"`.

The metadata advertises a historical range; that range was not downloaded or independently verified. Metadata does not declare numeric precision, a formal JSON Schema, or field nullability.

## Verified row fields and types

Every observed row value was a **JSON string**, including identifiers and numeric-looking measures. No native JSON numbers were observed in rows. The profiler records wire types separately from decimal parseability, preserving identifiers unchanged.

| Exact field | Routes | Observed meaning / examples | Proposed handling |
| --- | --- | --- | --- |
| `period` | All | Daily dates, e.g. `"2026-10-20"` | Preserve raw date string; require valid `YYYY-MM-DD` for a usable key. No timestamp/timezone is provided. |
| `capacity` | All | `"100056.7"` nationally; `"3755.8"` for Browns Ferry facility; `"1254.8"` for its generator 1 | Numeric decimal text; all sampled values parse as finite decimals. |
| `outage` | All | `"19859.767"` nationally; `"11.071"` at Clinton; `"0"` also observed | Numeric decimal text. Zero is a value, not missing data. |
| `percentOutage` | All | `"19.85"` nationally; `"59.04"` at Millstone; `"0"` also observed | Numeric decimal text in percent units. Do not assume it is a 0–1 fraction. |
| `capacity-units` | All | Always `"megawatts"` in this sample | Preserve/check against metadata. |
| `outage-units` | All | Always `"megawatts"` in this sample | Preserve/check against metadata. |
| `percentOutage-units` | All | Always `"percent"` in this sample | Preserve/check against metadata. |
| `facility` | Facility, generator | Plant codes, e.g. `"46"`, `"204"`, `"210"` | Keep as an opaque string; do not derive an integer schema from this sample. |
| `facilityName` | Facility, generator | `"Browns Ferry"`, `"Clinton Power Station"`, `"Wolf Creek Generating Station"` | Descriptive label, not identity. |
| `generator` | Generator only | `"1"`, `"2"`, `"3"` | Opaque string scoped to a facility. |

The national sample had exactly seven fields per row, facility nine, and generator ten. There was no additional generator-name field in these responses. The script discovers measures and identifier facets rather than assuming extra row fields.

### Required-field proposal

This is a proposed future validation policy, **not an EIA guarantee and not implemented as an extraction pipeline**:

- For row identity: require nonempty `period`, plus `facility` and/or `generator` according to the natural-key table. Validate the daily date format. Missing key values should be reported rather than silently assigned substitutes.
- For this requested three-measure projection: expect the presence of `capacity`, `outage`, `percentOutage`, and their three `*-units` fields. Preserve explicit nulls and distinguish them from absent fields and `""`; do not replace unknown values with zero.
- Expect `facilityName` on facility/generator responses because it was consistently returned, but do not require a nonempty display name to establish identity.
- For the exploration response envelope: require an object at `response`, a list of row objects at `response.data`, and a nonnegative integer/digit string at `response.total`. The tool also checks metadata measure/facet definitions and advertised daily frequency. Unknown additional fields remain visible to inspection.

Non-null measurements, fixed precision, nonnegative values, identifier character sets, and mathematical relationships among measures have not been established as universal constraints.

## Dates, pagination, and nulls

The successful run selected all three measures, `frequency=daily`, `start=2026-10-14`, `end=2026-10-20`, `length=3`, and offsets **0 and 3**. Sorting was explicitly `period desc`, followed by each advertised identifier facet ascending. The upper date bound came from metadata; the start is six days earlier, making a seven-day inclusive window.

| Route | Rows downloaded | Sample date range | `response.total` on both pages | Duplicates under proposed key |
| --- | --- | --- | --- | --- |
| National | 6 | 2026-10-15 through 2026-10-20 | `"7"` | 0 |
| Facility | 6 | 2026-10-20 only | `"665"` | 0 |
| Generator | 6 | 2026-10-20 only | `"665"` | 0 |

Each page returned three rows; the two pages did not overlap under the proposed keys. The national second page continued with October 17–15. Facility and generator second pages continued through identifiers on October 20. All routes still advertised more data after the second page; the script deliberately stopped. The reported totals count the filtered query, not the number downloaded or full-history size. The equal facility/generator totals are recorded as returned; their correctness and interpretation were not independently verified by enumerating entities or downloading all rows.

Each observed data response had `total` (a digit string), `dateFormat` (`"YYYY-MM-DD"`), `frequency` (`"daily"`), `description` (string), and `data` (row array). The report retains `apiVersion` separately. Request parameters such as offset and length are retained locally without the key; they should not be mistaken for guaranteed server response fields.

Across all **18 rows**, every observed field had **0 missing values, 0 JSON nulls, and 0 empty strings**. All 54 measure values parsed as finite decimals; all 18 periods parsed as daily ISO dates. This small recent sample cannot establish historical null behavior, suppression/sentinel strings, or schema stability. Mocked tests explicitly exercise nulls, missing fields, empty/non-numeric values, malformed dates, and incomplete/duplicate keys.

## Network observations and handling

The sandbox initially prevented outbound connections. After network permission, metadata requests succeeded. A subsequent pooled-client run encountered read timeouts while awaiting HTTP response headers, including with an increased timeout. DNS resolved both EIA hosts; the public website returned 200 and an unauthenticated API call returned 403 promptly. An isolated authenticated metadata request completed DNS/TCP/TLS and returned 200 in about 0.34 seconds.

Disabling HTTP connection reuse (`max_keepalive_connections=0`) and using the bounded seven-day query produced a successful complete exploration run with a 20-second timeout. This is consistent with an intermittent connection-reuse/server/network issue; the precise underlying cause is unresolved. The script keeps fresh connections as a simple workaround and does not retry indefinitely or disable TLS verification.

Missing credentials, connection/DNS/timeouts, non-200 statuses (including redirects), API error objects, non-JSON/nonstandard JSON, malformed metadata, malformed data arrays/totals, and oversized pages are handled without printing credential-bearing URLs or remote error bodies. Errors are reported per route; other routes are still attempted. Successful metadata/pages are retained when a later page fails. Any route error returns exit status 1. Interruptions return 130 without a traceback.

## Remaining uncertainties

- Global/historical natural-key uniqueness, identifier stability and reuse, and historical unit/field changes.
- Null/suppression conventions outside this recent sample; valid ranges and precision of measurements.
- Whether metadata boundaries and filtered totals accurately reflect all underlying rows; the same facility and generator count deserves further bounded investigation.
- Whether row revisions can shift offset pagination despite fixed dates and explicit ordering. This probe does not provide snapshot isolation.
- Full facet catalogs, plant/generator lifecycle history, geographic attributes, and publication/revision schedules were not fetched or inferred.
- The root cause of intermittent pooled-connection timeouts remains unproven; fresh connections worked for the retained run.

No production schema, database tables, extraction pipeline, or full dataset was created during this exploration stage. The subsequent [local extraction implementation and live validation](EIA_EXTRACTION.md) found that the facility route returned 55 rows for 2026-10-20 while reporting a total of 95; that mismatch prevents publication under the extractor's strict completeness policy.
