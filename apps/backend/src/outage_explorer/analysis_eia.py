"""Isolated, bounded EIA evidence collection and offline diagnostic replay.

This module never calls publication, refresh, audit recovery, or watermark code.
Returned rows can be analysed despite inconsistent totals, explicitly as unverified
source evidence. They are never represented as an accepted production snapshot.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import time

import duckdb
import httpx
import pyarrow as pa

from outage_explorer.analytics import VIEWS
from outage_explorer.diagnostics import find_anomalies, reconcile_daily, reconcile_facilities
from outage_explorer.explore_eia import DEFAULT_ENV, ROUTES, ExplorationError, load_api_key, metadata_fields, read_page, safe_json
from outage_explorer.extract_eia import ExtractionError, IDENTIFIERS, MEASURES, parse_date, request_body, schema_for, validate_record


def encoded(value) -> str:
    return json.dumps(value, default=str, sort_keys=True, allow_nan=False, separators=(',', ':'))


def digest(value) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def analysis_destination(path: Path) -> None:
    # A named workspace separates diagnostics from data/eia production roots.
    if 'eia-analysis' not in path.resolve().parent.parts or path.name == 'current.json':
        raise ExtractionError('Write outputs only inside a dedicated eia-analysis directory.')


def save_source(path: Path, source: dict, key: str) -> None:
    analysis_destination(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    # Exclude echoes recursively and redact even unexpected appearances of the key.
    content = safe_json(json.loads(encoded(source)), key).encode()
    temporary.write_bytes(gzip.compress(content, mtime=0))
    os.replace(temporary, path)


def data_query(route: str, start: date, end: date) -> dict:
    query = {'frequency': 'daily', 'start': str(start), 'end': str(end), 'length': 1000}
    query.update({f'data[{i}]': field for i, field in enumerate(MEASURES)})
    for i, field in enumerate(('period', *IDENTIFIERS[route])):
        query[f'sort[{i}][column]'] = field
        query[f'sort[{i}][direction]'] = 'asc'
    return query


def collect(client: httpx.Client, key: str, path: Path, *, end: date | None = None,
            sleep=time.sleep, request_budget: int = 36, resume_source: dict | None = None) -> dict:
    """At most 30 days, 1000 rows/page, 8 pages/route, 2 attempts/request.

    Each successful envelope is checkpointed before the next request. Failures are
    retained safely; no response or partial window is presented as complete.
    """
    analysis_destination(path)
    if path.exists():
        raise ExtractionError('Evidence destination already exists; use a fresh filename.')
    if not key.strip():
        raise ExtractionError('Missing EIA credential.')
    for name in ['httpx', 'httpcore', *logging.root.manager.loggerDict]:
        if name == 'httpx' or name.startswith('httpx.') or name == 'httpcore' or name.startswith('httpcore.'):
            logging.getLogger(name).disabled = True
    source = {'format_version': 1, 'purpose': 'NON_PRODUCTION_UNVERIFIED_EVIDENCE',
              'collected_at_utc': datetime.now(timezone.utc).isoformat(), 'request_budget': request_budget,
              'request_count': 0, 'retries': [], 'routes': {r: {'pages': [], 'failures': []} for r in ROUTES}}
    if resume_source is not None:
        checked = analyse(resume_source)
        if checked['status'] == 'invalid_records_analysis_withheld':
            raise ExtractionError('Cannot resume evidence containing invalid records.')
        source = deepcopy(resume_source)
        if source['request_count'] >= source['request_budget']:
            raise ExtractionError('Evidence request budget exhausted.')
        for entry in source['routes'].values():
            entry.setdefault('failure_history', []).extend(entry['failures'])
            entry['failures'] = []
        source.setdefault('resumed_at_utc', []).append(datetime.now(timezone.utc).isoformat())
    def before_request(_):
        if source['request_count'] >= source['request_budget']:
            raise ExtractionError('Evidence request budget exhausted.')
        source['request_count'] += 1
        sleep(0.25)
    client.event_hooks['request'].append(before_request)
    def fetch(route, suffix, params):
        entry = source['routes'][route]
        try:
            body, version = request_body(client, route + suffix, key, params, attempts=2,
                                         backoff=1, sleep=sleep,
                                         emit=lambda event, **fields: source['retries'].append({'route': route, 'event': event, **fields}))
            record = {'query': params, 'api_version': version, 'response': body, 'sha256': digest(body)}
            return record
        except ExtractionError as exc:
            entry['failures'].append(str(exc))
            return None
        finally:
            save_source(path, source, key)
    try:
        for route in ROUTES:
            if 'metadata' in source['routes'][route]:
                continue
            metadata = fetch(route, '/', {})
            if metadata:
                source['routes'][route]['metadata'] = metadata
            save_source(path, source, key)
        if not all('metadata' in entry for entry in source['routes'].values()):
            return source
        latest = min(parse_date(entry['metadata']['response']['endPeriod']) for entry in source['routes'].values())
        if resume_source is not None and 'end' in source:
            if end is not None and str(end) != source['end']:
                raise ExtractionError('A resumed window cannot change its end date.')
            end = parse_date(source['end'])
        else:
            end = min(end, latest) if end else latest
        start = end - timedelta(days=29)
        if start < max(parse_date(entry['metadata']['response']['startPeriod']) for entry in source['routes'].values()):
            raise ExtractionError('Thirty requested days exceed advertised coverage.')
        source.update(start=str(start), end=str(end))
        for route in ROUTES:
            previous_pages = source['routes'][route]['pages']
            if previous_pages and previous_pages[-1]['response'].get('data') == []:
                continue
            query = data_query(route, start, end)
            offset = sum(len(page['response']['data']) for page in previous_pages)
            for _ in range(max(0, 8 - len(previous_pages))):
                page = fetch(route, '/data/', {**query, 'offset': offset})
                if page is None:
                    break
                source['routes'][route]['pages'].append(page)
                save_source(path, source, key)
                try:
                    rows, _ = read_page(page['response'], 1000)
                except ExplorationError as exc:
                    source['routes'][route]['failures'].append(str(exc))
                    break
                if not rows:
                    break  # Diagnostic evidence only; analyse() still flags total mismatch.
                offset += len(rows)
            else:
                source['routes'][route]['failures'].append('Diagnostic page budget exhausted.')
        return source
    finally:
        client.event_hooks['request'].remove(before_request)
        save_source(path, source, key)


def analyse(source: dict) -> dict:
    """Revalidate source evidence, build the existing projections, reuse diagnostics."""
    if source.get('format_version') != 1 or source.get('purpose') != 'NON_PRODUCTION_UNVERIFIED_EVIDENCE':
        raise ExtractionError('Unsupported analysis evidence format.')
    result = {'purpose': source['purpose'], 'production_eligible': False,
              'collected_at_utc': source.get('collected_at_utc'), 'request_count': source['request_count'],
              'retries': source['retries'], 'routes': {}, 'daily': [], 'facilities': [], 'anomalies': {}}
    if 'start' not in source or 'end' not in source:
        result['status'] = 'metadata_incomplete'
        result['routes'] = {route: {'failures': entry['failures']} for route, entry in source['routes'].items()}
        return result
    start, end = parse_date(source['start']), parse_date(source['end'])
    if (end - start).days != 29:
        raise ExtractionError('Evidence must describe exactly 30 consecutive days.')
    result.update(start=str(start), end=str(end), requested_days=30)
    validated = {}
    for route in ROUTES:
        entry = source['routes'][route]
        issues = list(entry['failures'])
        metadata = entry['metadata']
        if digest(metadata['response']) != metadata['sha256']:
            raise ExtractionError('Evidence checksum mismatch.')
        measures, facets = metadata_fields(metadata['response'])
        if set(facets) != set(IDENTIFIERS[route]) or not set(MEASURES) <= set(measures) or any(
                metadata['response']['data'][m].get('units') != unit for m, unit in MEASURES.items()):
            raise ExtractionError('Incompatible evidence metadata.')
        rows, keys, periods, totals = [], set(), set(), []
        terminal = False
        valid = True
        offset = 0
        for page in entry['pages']:
            if digest(page['response']) != page['sha256']:
                raise ExtractionError('Evidence checksum mismatch.')
            body, query = page['response'], page['query']
            if query != {**data_query(route, start, end), 'offset': offset}:
                raise ExtractionError('Evidence offsets/date filters are inconsistent.')
            if terminal:
                raise ExtractionError('Evidence contains pages after its terminal response.')
            if body.get('frequency') != 'daily' or body.get('dateFormat') != 'YYYY-MM-DD':
                raise ExtractionError('Unsupported evidence frequency/date format.')
            if page['api_version'] != metadata['api_version']:
                raise ExtractionError('Evidence API version changed.')
            try:
                incoming, total = read_page(body, 1000)
            except ExplorationError as exc:
                valid = False
                issues.append(str(exc))
                break
            totals.append(total)
            terminal = not incoming
            for index, row in enumerate(incoming):
                try:
                    record, key = validate_record(row, route, start, end)
                    if key in keys:
                        raise ExtractionError('Duplicate candidate natural key.')
                    keys.add(key)
                    periods.add(record['period'])
                    rows.append(record)
                except ExtractionError as exc:
                    valid = False
                    issues.append(f'Invalid record at offset {offset}, row {index}: {exc}')
            offset += len(incoming)
        if not terminal:
            issues.append('No terminal empty page observed; traversal incomplete.')
        if len(set(totals)) != 1:
            issues.append('Reported total missing or changed during traversal.')
        count_agrees = bool(totals) and len(set(totals)) == 1 and offset == totals[0]
        if not count_agrees:
            issues.append('Returned row count disagrees with reported total; source completeness unresolved.')
        missing = [str(start + timedelta(days=i)) for i in range(30) if start + timedelta(days=i) not in periods]
        if missing:
            issues.append('Requested date coverage is incomplete.')
        result['routes'][route] = {'received_rows': offset, 'validated_rows': len(rows), 'unique_keys': len(keys),
                                  'days_covered': len(periods), 'missing_dates': missing, 'reported_totals': totals,
                                  'terminal_empty_page': terminal, 'count_agrees': count_agrees,
                                  'valid_records': valid, 'failures': issues, 'failure_history': entry.get('failure_history', [])}
        validated[route] = rows
    if not all(entry['valid_records'] for entry in result['routes'].values()):
        result['status'] = 'invalid_records_analysis_withheld'
        return result
    result['status'] = 'diagnostic_only_completeness_unverified'
    with duckdb.connect(':memory:') as db:
        for name, (route, identifiers) in VIEWS.items():
            db.from_arrow(pa.Table.from_pylist(validated[route], schema=schema_for(route))).project(
                f'period AS outage_date, {identifiers} capacity AS capacity_mw, outage AS outage_mw, percentOutage AS percent_outage'
            ).create_view(name)
        result['daily'] = reconcile_daily(db, start, end)
        for row in result['daily']:
            for measure, suffix in [('outage', 'mw'), ('capacity', 'capacity_mw')]:
                left, right = row[f'generator_{measure}_mw'], row[f'facility_{measure}_mw']
                row[f'generator_minus_facility_{suffix}'] = left - right if left is not None and right is not None else None
        result['facilities'] = reconcile_facilities(db, start, end)
        # No arbitrary change threshold; ordinary operating transitions are not errors.
        result['anomalies'] = find_anomalies(db, start, end)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    fetch = sub.add_parser('fetch')
    fetch.add_argument('--output', type=Path, required=True, help='Fresh non-production .json.gz evidence file')
    fetch.add_argument('--end', type=parse_date)
    fetch.add_argument('--resume-from', type=Path, help='Resume verified partial evidence into a fresh output file')
    replay = sub.add_parser('analyse')
    replay.add_argument('--input', type=Path, required=True)
    replay.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'fetch':
            key = load_api_key(DEFAULT_ENV)
            resume_source = json.loads(gzip.decompress(args.resume_from.read_bytes())) if args.resume_from else None
            with httpx.Client(timeout=20, follow_redirects=False, limits=httpx.Limits(max_keepalive_connections=0)) as client:
                source = collect(client, key, args.output, end=args.end, resume_source=resume_source)
            print(encoded({'saved': 'non-production evidence', 'requests': source['request_count'],
                           'failures': {r: e['failures'] for r, e in source['routes'].items()}}))
            return 0 if 'start' in source and not any(e['failures'] for e in source['routes'].values()) else 1
        source = json.loads(gzip.decompress(args.input.read_bytes()))
        result = analyse(source)
        analysis_destination(args.output)
        if args.input.resolve() == args.output.resolve():
            raise ExtractionError('Analysis output must not overwrite source evidence.')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, default=str, indent=2, allow_nan=False) + '\n')
        print(encoded({'status': result['status'], 'routes': result['routes'],
                       'anomaly_counts': {kind: len(rows) for kind, rows in result['anomalies'].items()}}))
        return 0
    except (ExtractionError, ExplorationError, OSError, ValueError, KeyError, TypeError, duckdb.Error, pa.ArrowException, httpx.RequestError):
        print('Analysis operation failed; partial evidence is retained where available. Details withheld.')
        return 1
    except KeyboardInterrupt:
        print('Analysis interrupted; partial evidence retained.')
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
