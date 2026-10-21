"""Bounded, manual schema exploration; not a production extraction pipeline."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any

from dotenv import load_dotenv
import httpx

BASE_URL = "https://api.eia.gov/v2/nuclear-outages/"
ROUTES = ("us-nuclear-outages", "facility-nuclear-outages", "generator-nuclear-outages")
DEFAULT_ENV = Path(__file__).resolve().parents[2] / ".env"


class ExplorationError(Exception):
    """An actionable error whose message contains no credentials or request URL."""


def load_api_key(env_file: Path = DEFAULT_ENV) -> str:
    """Prefer the exported environment; do not interpolate or override its values."""
    load_dotenv(env_file, override=False, interpolate=False)
    key = os.environ.get("EIA_API_KEY", "").strip()
    if not key:
        raise ExplorationError("Missing EIA_API_KEY; export it or configure apps/backend/.env.")
    return key


def safe_json(value: Any, key: str) -> str:
    """Remove echoed request credentials, including unexpected nested echoes."""
    def scrub(item: Any) -> Any:
        if isinstance(item, dict):
            return {k: scrub(v) for k, v in item.items() if k.lower() not in {"request", "api_key"}}
        if isinstance(item, list):
            return [scrub(v) for v in item]
        if isinstance(item, str):
            return item.replace(key, "[REDACTED]") if key else item
        return item

    rendered = json.dumps(scrub(value), indent=2, sort_keys=True, allow_nan=False)
    return rendered.replace(key, "[REDACTED]") if key else rendered


def fetch_response(client: httpx.Client, route: str, key: str, params: dict[str, Any] | None = None) -> dict:
    """Keep HTTPX exception URLs and EIA's echoed key out of diagnostics."""
    try:
        response = client.get(BASE_URL + route, params={"api_key": key, **(params or {})})
    except httpx.RequestError as exc:
        cause = exc
        dns = False
        while cause is not None:
            dns |= type(cause).__name__ == "gaierror"
            cause = cause.__cause__
        detail = "DNS resolution failed" if dns else type(exc).__name__
        raise ExplorationError(f"Network failure ({detail}); check DNS, connectivity, proxy, and network permissions.") from None
    if response.status_code != 200:
        raise ExplorationError(f"HTTP {response.status_code}; check credentials (401/403), rate limits (429), or EIA availability (5xx).")
    try:
        def reject_constant(value: str) -> None:
            raise ValueError("Non-standard JSON numeric constant")

        payload = json.loads(response.content, parse_constant=reject_constant)
    except ValueError:
        raise ExplorationError("Malformed response: expected JSON.") from None
    if not isinstance(payload, dict) or payload.get("error"):
        raise ExplorationError("Malformed/API error response: expected a successful response object.")
    body = payload.get("response")
    if not isinstance(body, dict):
        raise ExplorationError("Malformed response: response must be an object.")
    # Deliberately discard the top-level request echo before retaining any data.
    return {"response": body, "apiVersion": payload.get("apiVersion")}


def metadata_fields(body: dict) -> tuple[list[str], list[str]]:
    measures = body.get("data")
    facets = body.get("facets")
    frequencies = body.get("frequency")
    if not isinstance(measures, dict) or not measures or not all(isinstance(k, str) and isinstance(v, dict) for k, v in measures.items()):
        raise ExplorationError("Malformed metadata: expected nonempty data measure definitions.")
    if not isinstance(facets, list) or not all(isinstance(f, dict) and isinstance(f.get("id"), str) for f in facets):
        raise ExplorationError("Malformed metadata: expected facet definitions.")
    if not isinstance(frequencies, list) or not any(isinstance(f, dict) and f.get("id") == "daily" for f in frequencies):
        raise ExplorationError("Malformed/unsupported metadata: daily frequency is not advertised.")
    return list(measures), [f["id"] for f in facets]


def read_page(body: dict, length: int) -> tuple[list[dict], int]:
    rows = body.get("data")
    total = body.get("total")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ExplorationError("Malformed data response: data must be a list of objects.")
    if len(rows) > length:
        raise ExplorationError("Response exceeded the requested sample size; stopped.")
    if isinstance(total, bool) or not re.fullmatch(r"\d+", str(total)):
        raise ExplorationError("Malformed data response: total must be a nonnegative integer or digit string.")
    if int(total) < len(rows):
        raise ExplorationError("Malformed data response: total is smaller than the returned row count.")
    return rows, int(total)


def inspect_rows(rows: list[dict], key_fields: list[str]) -> dict:
    """Describe observed JSON values; never coerce identifiers or infer SQL types."""
    fields = {}
    for name in sorted({name for row in rows for name in row}):
        values = [row[name] for row in rows if name in row]
        nonnull = [v for v in values if v is not None]
        examples = []
        for value in nonnull:
            if value not in examples and len(examples) < 3:
                examples.append(value)
        numeric = 0
        for value in nonnull:
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                try:
                    numeric += int(Decimal(str(value)).is_finite())
                except InvalidOperation:
                    pass
        fields[name] = {
            "json_types": sorted({type(v).__name__ for v in values}),
            "missing": len(rows) - len(values), "null": len(values) - len(nonnull),
            "empty_strings": sum(v == "" for v in nonnull),
            "numeric_parseable": numeric, "examples": examples,
            "distinct_nonnull": len({json.dumps(v, sort_keys=True) for v in nonnull}),
        }
    valid_dates = []
    invalid_dates = 0
    for row in rows:
        period = row.get("period")
        try:
            if not isinstance(period, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", period):
                raise ValueError
            valid_dates.append(date.fromisoformat(period).isoformat())
        except ValueError:
            invalid_dates += 1
    incomplete = sum(any(row.get(f) in (None, "") for f in key_fields) for row in rows)
    keys = [json.dumps([row[f] for f in key_fields]) for row in rows if all(row.get(f) not in (None, "") for f in key_fields)]
    return {
        "row_count": len(rows), "fields": fields,
        "period": {"min": min(valid_dates, default=None), "max": max(valid_dates, default=None), "invalid_or_missing": invalid_dates},
        "candidate_key": {"fields": key_fields, "incomplete_rows": incomplete, "duplicate_rows": len(keys) - len(set(keys)), "scope": "sample only"},
    }


def explore_route(client: httpx.Client, route: str, key: str, length: int) -> dict:
    envelope = fetch_response(client, route + "/", key)
    metadata = envelope["response"]
    measures, facets = metadata_fields(metadata)
    params: dict[str, Any] = {"frequency": "daily", "length": length}
    for index, measure in enumerate(measures):
        params[f"data[{index}]"] = measure
    for index, column in enumerate(["period", *facets]):
        params[f"sort[{index}][column]"] = column
        params[f"sort[{index}][direction]"] = "desc" if index == 0 else "asc"
    # Fix the upper date bound from metadata so a new day's publication cannot
    # shift the second page. No unbounded pagination or historical backfill.
    if isinstance(metadata.get("endPeriod"), str):
        params["end"] = metadata["endPeriod"]
        try:
            params["start"] = (date.fromisoformat(metadata["endPeriod"]) - timedelta(days=6)).isoformat()
        except ValueError:
            raise ExplorationError("Malformed metadata: endPeriod is not a daily ISO date.") from None
    pages = []
    rows = []
    error = None
    for offset in (0, length):
        query = {**params, "offset": offset}
        try:
            page = fetch_response(client, route + "/data/", key, query)
            page_rows, total = read_page(page["response"], length)
        except ExplorationError as exc:
            error = f"Data page at offset {offset}: {exc}"
            break
        pages.append({"query": query, "apiVersion": page["apiVersion"], "response": page["response"], "returned_rows": len(page_rows), "has_more": offset + len(page_rows) < total})
        rows.extend(page_rows)
        if offset + len(page_rows) >= total or not page_rows:
            break
    result = {"metadata": metadata, "apiVersion": envelope["apiVersion"], "pages": pages, "inspection": inspect_rows(rows, ["period", *facets])}
    if error:
        result["error"] = error
    return result


def explore(client: httpx.Client, key: str, length: int = 3) -> dict:
    if not key.strip():
        raise ExplorationError("Missing EIA_API_KEY.")
    if not 1 <= length <= 10:
        raise ExplorationError("Sample length must be between 1 and 10.")
    result: dict[str, Any] = {"fetched_at_utc": datetime.now(timezone.utc).isoformat(), "base_url": BASE_URL, "page_length": length, "max_pages_per_route": 2, "routes": {}}
    for route in ROUTES:
        try:
            result["routes"][route] = explore_route(client, route, key, length)
        except ExplorationError as exc:
            result["routes"][route] = {"error": f"Metadata: {exc}"}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--length", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--timeout", type=int, choices=range(1, 61), default=45, metavar="SECONDS")
    parser.add_argument("--output", type=Path, help="Write a sanitized JSON report instead of stdout.")
    args = parser.parse_args(argv)
    # HTTPX logs full URLs at INFO, including EIA's required query credential.
    for logger in ("httpx", "httpcore"):
        logging.getLogger(logger).disabled = True
    try:
        key = load_api_key(args.env_file)
        with httpx.Client(
            timeout=args.timeout,
            follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        ) as client:
            result = explore(client, key, args.length)
        rendered = safe_json(result, key) + "\n"
        if args.output:
            # Never allow output to replace a credential file.
            if args.output.resolve().name.startswith(".env") or args.output.resolve() == args.env_file.resolve():
                raise ExplorationError("Choose an output path other than an environment file.")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered)
        else:
            print(rendered, end="")
        return int(any("error" in r for r in result["routes"].values()))
    except ExplorationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("Could not read the environment file or write the output file.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Exploration interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
