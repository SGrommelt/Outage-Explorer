"""Extract one explicit EIA date window into an atomically published local snapshot."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from email.utils import parsedate_to_datetime
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Callable
from uuid import uuid4

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from outage_explorer.explore_eia import (
    BASE_URL, DEFAULT_ENV, ROUTES, ExplorationError, load_api_key,
    metadata_fields, read_page, safe_json,
)

MEASURES = {"capacity": "megawatts", "outage": "megawatts", "percentOutage": "percent"}
IDENTIFIERS = {
    "us-nuclear-outages": (),
    "facility-nuclear-outages": ("facility",),
    "generator-nuclear-outages": ("facility", "generator"),
}
DECIMAL_TYPE = pa.decimal128(24, 6)
FACILITY_POLICY = "facility_terminal_cross_grain_v1"
SOURCE_CAVEAT = ("Source completeness is unverified. Facility reported totals may differ from delivered rows; "
                 "terminal traversal and exact cross-grain agreement establish internal validity, not independent EIA coverage.")


class ExtractionError(Exception):
    """Safe diagnostic: never include raw records, HTTP exception text, or URLs."""


def parse_date(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ExtractionError("Expected an ISO date in YYYY-MM-DD format.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ExtractionError("Invalid calendar date.") from None


def schema_for(route: str) -> pa.Schema:
    fields = [pa.field("period", pa.date32(), nullable=False)]
    for field in IDENTIFIERS[route]:
        fields.append(pa.field(field, pa.string(), nullable=False))
    if IDENTIFIERS[route]:
        fields.append(pa.field("facilityName", pa.string()))
    for measure in MEASURES:
        fields.extend([
            pa.field(measure, DECIMAL_TYPE, nullable=False),
            pa.field(measure + "-units", pa.string(), nullable=False),
        ])
    return pa.schema(fields)


def numeric(value: object, field: str) -> Decimal:
    # Fixed precision is a storage policy, not an inferred EIA guarantee. Reject
    # overflow/precision loss instead of rounding, coercing nulls, or substituting 0.
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or value == "":
        raise ExtractionError(f"Invalid numeric measure: {field}.")
    try:
        with localcontext() as context:
            context.prec = 50
            number = Decimal(str(value))
            if not number.is_finite() or abs(number) >= Decimal("1e18"):
                raise ValueError
            scaled = number.quantize(Decimal("0.000001"))
            if scaled != number:
                raise ValueError
            return scaled
    except (InvalidOperation, ValueError):
        raise ExtractionError(f"Nonfinite, invalid, or unrepresentable measure: {field}.") from None


def validate_record(row: dict, route: str, start: date, end: date) -> tuple[dict, tuple]:
    expected = set(schema_for(route).names)
    if set(row) - expected:
        raise ExtractionError("Unexpected row fields; inspect schema drift before extracting.")
    period = parse_date(row.get("period"))
    if not start <= period <= end:
        raise ExtractionError("Record date is outside the requested window.")
    result = {"period": period}
    for field in IDENTIFIERS[route]:
        value = row.get(field)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ExtractionError(f"Missing or invalid key identifier: {field}.")
        result[field] = value
    if IDENTIFIERS[route]:
        name = row.get("facilityName")
        if name is not None and not isinstance(name, str):
            raise ExtractionError("Invalid facilityName; expected a string or null.")
        result["facilityName"] = name
    for field, units in MEASURES.items():
        if field not in row or field + "-units" not in row:
            raise ExtractionError(f"Missing required measure or units: {field}.")
        if row[field + "-units"] != units:
            raise ExtractionError(f"Unexpected units: {field}.")
        result[field] = numeric(row[field], field)
        result[field + "-units"] = units
    key = (period, *(result[field] for field in IDENTIFIERS[route]))
    return result, key


def retry_delay(header: str | None, attempt: int, backoff: float) -> float:
    delay = min(30.0, backoff * 2 ** attempt)
    if header:
        try:
            seconds = float(header)
            if not math.isfinite(seconds):
                raise ValueError
        except ValueError:
            try:
                when = parsedate_to_datetime(header)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                seconds = (when - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = 0
        delay = min(30.0, max(delay, seconds))
    return delay


def request_body(
    client: httpx.Client, route: str, key: str, params: dict,
    *, attempts: int, backoff: float, sleep: Callable, emit: Callable,
) -> tuple[dict, str | None]:
    for attempt in range(attempts):
        reason = None
        retry_after = None
        try:
            response = client.get(BASE_URL + route, params={**params, "api_key": key})
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            reason = "network"
        else:
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                reason = f"http_{response.status_code}"
                retry_after = response.headers.get("Retry-After")
            elif response.status_code != 200:
                raise ExtractionError(f"Non-retryable HTTP status {response.status_code}.")
            else:
                def reject_constant(_: str) -> None:
                    raise ValueError
                try:
                    payload = json.loads(response.content, parse_float=Decimal, parse_constant=reject_constant)
                except ValueError:
                    raise ExtractionError("Malformed JSON response.") from None
                if not isinstance(payload, dict) or payload.get("error") or payload.get("warning"):
                    raise ExtractionError("API returned an error or warning; response not accepted.")
                body = payload.get("response")
                if not isinstance(body, dict) or body.get("error") or body.get("warning"):
                    raise ExtractionError("Malformed/error/warning response object.")
                version = payload.get("apiVersion")
                return body, version if isinstance(version, str) else None
        if attempt + 1 == attempts:
            raise ExtractionError(f"Retry budget exhausted ({reason}, {attempts} attempts).")
        delay = retry_delay(retry_after, attempt, backoff)
        emit("retry", attempt=attempt + 1, reason=reason, delay_seconds=delay)
        sleep(delay)
    raise AssertionError("Unreachable retry state")


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path: Path, value: dict) -> None:
    with path.open("w") as output:
        json.dump(value, output, sort_keys=True, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def extract_route(
    client: httpx.Client, route: str, key: str, start: date, end: date,
    path: Path, page_size: int, max_pages: int, attempts: int,
    backoff: float, sleep: Callable, emit: Callable,
) -> dict:
    def fetch(suffix: str, params: dict) -> tuple[dict, str | None]:
        return request_body(client, route + suffix, key, params, attempts=attempts,
                            backoff=backoff, sleep=sleep,
                            emit=lambda event, **fields: emit(event, route=route, **fields))

    metadata, version = fetch("/", {})
    try:
        measures, facets = metadata_fields(metadata)
    except ExplorationError:
        raise ExtractionError("Unsupported or malformed route metadata.") from None
    if set(facets) != set(IDENTIFIERS[route]) or not set(MEASURES) <= set(measures):
        raise ExtractionError("Route dimensions/measures changed; schema review required.")
    for field, units in MEASURES.items():
        if metadata["data"][field].get("units") != units:
            raise ExtractionError("Route measure units changed; schema review required.")
    query = {"frequency": "daily", "start": start.isoformat(), "end": end.isoformat(), "length": page_size}
    for index, field in enumerate(MEASURES):
        query[f"data[{index}]"] = field
    for index, field in enumerate(("period", *IDENTIFIERS[route])):
        query[f"sort[{index}][column]"] = field
        query[f"sort[{index}][direction]"] = "asc"
    keys = set()
    periods = set()
    offset = 0
    expected_total = None
    schema = schema_for(route)
    facility = route == "facility-nuclear-outages"
    terminal = False
    previous_period = None
    with pq.ParquetWriter(path, schema, compression="zstd") as writer:
        for page_index in range(max_pages):
            body, page_version = fetch("/data/", {**query, "offset": offset})
            if body.get("frequency") != "daily" or body.get("dateFormat") != "YYYY-MM-DD":
                raise ExtractionError("Unexpected response frequency or date format.")
            if version and page_version and version != page_version:
                raise ExtractionError("API version changed during extraction.")
            try:
                rows, total = read_page(body, page_size)
            except ExplorationError:
                raise ExtractionError("Malformed or oversized data page.") from None
            if expected_total is None:
                expected_total = total
            if total != expected_total:
                raise ExtractionError("Pagination total changed during extraction; retry the window.")
            if total == 0:
                raise ExtractionError("Empty route result; refusing to publish an empty snapshot.")
            if facility and not rows and offset:
                terminal = True
                break
            if not rows or offset + len(rows) > total:
                raise ExtractionError("Incomplete or inconsistent pagination.")
            validated = []
            for row_index, row in enumerate(rows):
                try:
                    record, natural_key = validate_record(row, route, start, end)
                except ExtractionError as exc:
                    emit("invalid_record", route=route, offset=offset, row_index=row_index)
                    raise ExtractionError(f"Invalid record at offset {offset}, row {row_index}: {exc}") from None
                if previous_period is not None and record['period'] < previous_period:
                    raise ExtractionError('Pagination period order regressed.')
                previous_period = record['period']
                if natural_key in keys:
                    raise ExtractionError("Duplicate candidate natural key; refusing ambiguous pagination/data.")
                keys.add(natural_key)
                periods.add(record["period"])
                validated.append(record)
            writer.write_table(pa.Table.from_pylist(validated, schema=schema))
            offset += len(rows)
            emit("page_complete", route=route, page=page_index + 1, rows=len(rows), records=offset, total=total)
            if offset == total and not facility:
                break
        else:
            raise ExtractionError("Page limit reached before completion; nothing published.")
    if len(periods) != (end - start).days + 1:
        raise ExtractionError("Daily coverage is incomplete for the requested window.")
    if pq.read_metadata(path).num_rows != offset or pq.read_schema(path) != schema:
        raise ExtractionError("Parquet validation failed.")
    with path.open("rb") as source:
        os.fsync(source.fileno())
    emit("route_complete", route=route, records=offset)
    return {"file": path.name, "records": offset, "pages": page_index + 1,
            "api_version": version, "candidate_key": ["period", *IDENTIFIERS[route]],
            "acceptance_policy": FACILITY_POLICY if facility else "strict_reported_total_v1",
            "reported_total": expected_total, "received_rows": offset, "unique_rows": len(keys),
            "terminal_empty_offset": offset if terminal else None,
            "count_agrees": offset == expected_total}


def validate_acceptance_metadata(routes: dict, validation: dict) -> None:
    """Validate optional new acceptance metadata without changing legacy manifests."""
    if (not isinstance(validation, dict) or validation.get('policy') != FACILITY_POLICY
            or validation.get('source_completeness') != 'unverified'
            or validation.get('caveat') != SOURCE_CAVEAT
            or any(validation.get(field) is not True for field in
                   ('facility_generator_keys_match', 'facility_generator_mw_match', 'daily_mw_match'))):
        raise ExtractionError('Invalid acceptance validation metadata.')
    for route in ROUTES:
        entry = routes[route]
        received, total = entry.get('received_rows'), entry.get('reported_total')
        facility = route == 'facility-nuclear-outages'
        if (type(received) is not int or type(total) is not int or not 0 < received <= total
                or type(entry.get('unique_rows')) is not int or entry['unique_rows'] != received
                or entry.get('acceptance_policy') != (FACILITY_POLICY if facility else 'strict_reported_total_v1')
                or entry.get('count_agrees') is not (received == total)
                or (facility and entry.get('terminal_empty_offset') != received)
                or (not facility and received != total)):
            raise ExtractionError('Invalid route acceptance metadata.')
    facility = routes['facility-nuclear-outages']
    mismatch = not facility['count_agrees']
    if (validation.get('facility_count_exception') is not mismatch
            or (mismatch and facility['reported_total'] != routes['generator-nuclear-outages']['received_rows'])):
        raise ExtractionError('Invalid Facility count-exception metadata.')


def validate_window(rows: dict[str, list[dict]], routes: dict) -> dict:
    """Gate publication using the same fetched window, never cumulative/other vintages."""
    facility_route = 'facility-nuclear-outages'
    facility = routes[facility_route]
    if facility['reported_total'] != facility['received_rows']:
        if facility['reported_total'] != routes['generator-nuclear-outages']['received_rows']:
            raise ExtractionError('Facility count exception does not match the validated generator count.')
    with localcontext() as context:
        context.prec = 60
        def groups(records, fields):
            result = {}
            for row in records:
                key = tuple(row[f] for f in fields)
                totals = result.setdefault(key, [Decimal(0), Decimal(0)])
                for i, measure in enumerate(('capacity', 'outage')):
                    totals[i] += row[measure]
            return result
        facilities = groups(rows[facility_route], ('period', 'facility'))
        generators = groups(rows['generator-nuclear-outages'], ('period', 'facility'))
        if facilities.keys() != generators.keys():
            raise ExtractionError('Facility/generator date-key coverage differs.')
        if facilities != generators:
            raise ExtractionError('Facility/generator capacity or outage totals differ.')
        national = groups(rows['us-nuclear-outages'], ('period',))
        if any(groups(records, ('period',)) != national for records in rows.values()):
            raise ExtractionError('National/facility/generator daily capacity or outage totals differ.')
    return {'policy': FACILITY_POLICY, 'source_completeness': 'unverified', 'caveat': SOURCE_CAVEAT,
            'facility_generator_keys_match': True, 'facility_generator_mw_match': True,
            'daily_mw_match': True, 'facility_count_exception': not facility['count_agrees']}


def extract(
    client: httpx.Client, key: str, start: date, end: date, output_dir: Path,
    *, page_size: int = 500, max_pages: int = 10000, attempts: int = 4,
    backoff: float = 1.0, sleep: Callable = time.sleep, emit: Callable | None = None,
) -> dict:
    if not key.strip() or start > end:
        raise ExtractionError("A credential and valid inclusive date window are required.")
    if not 1 <= page_size <= 5000 or not 1 <= attempts <= 6 or not 1 <= max_pages <= 100000:
        raise ExtractionError("Invalid page size, attempt count, or page limit.")
    if not math.isfinite(backoff) or not 0 <= backoff <= 30:
        raise ExtractionError("Backoff must be finite and between 0 and 30 seconds.")
    # Suppress credential-bearing HTTP logs even when invoked as a library.
    for name in ["httpx", "httpcore", *logging.root.manager.loggerDict]:
        if name == "httpx" or name.startswith("httpx.") or name == "httpcore" or name.startswith("httpcore."):
            logging.getLogger(name).disabled = True
    started = time.monotonic()

    def report(event: str, **fields) -> None:
        entry = {"event": event, "elapsed_seconds": round(time.monotonic() - started, 3), **fields}
        sanitized = json.loads(safe_json(entry, key))
        if emit is None:
            print(json.dumps(sanitized, sort_keys=True), file=sys.stderr, flush=True)
        else:
            emit(sanitized)

    window_dir = output_dir / f"{start.isoformat()}_{end.isoformat()}"
    run_id = uuid4().hex
    pointer_temp = window_dir / f".current-{run_id}.json"
    active_route = None
    published = False
    report("extraction_started", start=start.isoformat(), end=end.isoformat(), page_size=page_size)
    try:
        window_dir.mkdir(parents=True, exist_ok=True)
        runs_dir = window_dir / "runs"
        runs_dir.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=window_dir) as staging:
            stage = Path(staging)
            manifest = {"schema_version": 1, "start": start.isoformat(), "end": end.isoformat(),
                        "created_at_utc": datetime.now(timezone.utc).isoformat(), "routes": {}}
            failed_routes = []
            for route in ROUTES:
                active_route = route
                try:
                    manifest["routes"][route] = extract_route(
                        client, route, key, start, end, stage / f"{route}.parquet",
                        page_size, max_pages, attempts, backoff, sleep, report,
                    )
                except ExtractionError as exc:
                    failed_routes.append(f"{route}: {exc}")
                    report("route_failed", route=route, reason=str(exc))
            active_route = None
            if failed_routes:
                raise ExtractionError("No snapshot published. " + "; ".join(failed_routes))
            manifest['validation'] = validate_window(
                {r: pq.ParquetFile(stage / f'{r}.parquet').read().to_pylist() for r in ROUTES}, manifest['routes'])
            write_json(stage / "manifest.json", manifest)
            sync_directory(stage)
            final_dir = runs_dir / run_id
            os.replace(stage, final_dir)
            sync_directory(runs_dir)
            # The only publication point. Readers resolve this once, then open
            # immutable files in that run. Never glob across snapshot runs.
            write_json(pointer_temp, {"run": f"runs/{run_id}"})
            os.replace(pointer_temp, window_dir / "current.json")
            published = True
            sync_directory(window_dir)
        report("extraction_complete", records={r: v["records"] for r, v in manifest["routes"].items()})
        return manifest
    except (ExtractionError, KeyboardInterrupt, OSError, pa.ArrowException, httpx.RequestError):
        report("extraction_failed", route=active_route, published=published)
        raise
    finally:
        pointer_temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Inclusive YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Inclusive YYYY-MM-DD")
    parser.add_argument("--output-dir", type=Path, default=Path("data/eia"))
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=10000)
    parser.add_argument("--attempts", type=int, default=4, help="Total attempts per request, including the first")
    parser.add_argument("--backoff", type=float, default=1)
    parser.add_argument("--timeout", type=int, choices=range(1, 61), default=30, metavar="SECONDS")
    args = parser.parse_args(argv)
    try:
        start, end = parse_date(args.start), parse_date(args.end)
        key = load_api_key(args.env_file)
        with httpx.Client(timeout=args.timeout, follow_redirects=False,
                          limits=httpx.Limits(max_keepalive_connections=0)) as client:
            extract(client, key, start, end, args.output_dir, page_size=args.page_size,
                    max_pages=args.max_pages, attempts=args.attempts, backoff=args.backoff)
        return 0
    except (ExtractionError, ExplorationError) as exc:
        print(json.dumps({"event": "command_failed", "reason": str(exc)}), file=sys.stderr)
        return 1
    except (OSError, pa.ArrowException, httpx.RequestError):
        print(json.dumps({"event": "command_failed", "reason": "Local storage or HTTP operation failed; snapshot was not accepted."}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"event": "command_interrupted"}), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
