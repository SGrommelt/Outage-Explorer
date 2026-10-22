"""Isolated, bounded EIA evidence collection and offline diagnostic replay.

This module never calls publication, refresh, audit recovery, or watermark code.
Returned rows can be analysed despite inconsistent totals, explicitly as unverified
source evidence. They are never represented as an accepted production snapshot.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
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
from outage_explorer.extract_eia import ExtractionError, IDENTIFIERS, MEASURES, parse_date, request_body, schema_for, validate_record, numeric


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


def tukey_summary(values: list[Decimal]) -> dict:
    """Linear interpolation at (n-1)*p (type 7); strict 1.5-IQR fences.

    Empty samples have no fences. Singleton/constant samples have coincident
    fences and no outliers. This is descriptive screening, not significance.
    """
    ordered = sorted(values)
    if not ordered:
        return {'count': 0, 'q1': None, 'q3': None, 'upper_fence': None}
    with localcontext() as context:
        context.prec = 60
        def quantile(p):
            index = Decimal(len(ordered) - 1) * p
            lower = int(index)
            return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (index - lower)
        q1, q3 = quantile(Decimal('0.25')), quantile(Decimal('0.75'))
        return {'count': len(ordered), 'q1': q1, 'q3': q3,
                'upper_fence': q3 + Decimal('1.5') * (q3 - q1)}


def inspect_temporal(source: dict) -> dict:
    """Offline candidate inventory, not an assertion that capacity must be stable.

    The 0.005 percentage-point comparison is the nearest-two-decimal rounding
    interval motivated by the maximum displayed precision here, not a verified EIA contract.
    Neither ordinary transitions nor coordinated capacity changes are counted
    as independent source defects by this function.
    """
    baseline = analyse(source)
    if baseline['status'] != 'diagnostic_only_completeness_unverified':
        raise ExtractionError('Temporal inspection requires validated record evidence.')
    result = {'purpose': source['purpose'], 'production_eligible': False,
              'start': source['start'], 'end': source['end'],
              'classification': 'candidate_inventory_not_confirmed_source_anomalies',
              'rounding_assumption': 'nearest 0.01 percentage point; interval +/-0.005; not an EIA guarantee',
              'membership_changes': [], 'capacity_changes': [], 'capacity_decomposition': [],
              'label_changes': [], 'cross_grain_label_mismatches': [], 'missing_labels': [],
              'percentage_candidates': [], 'undefined_ratios': [], 'skipped_intervals': [],
              'source_percentage_decimal_places': {}, 'maximum_percentage_gaps': {},
              'baseline_anomaly_counts': {name: len(rows) for name, rows in baseline['anomalies'].items()}}
    result['route_counts'] = baseline['routes']
    observations = {}
    # analyse() checks checksums, exact queries, metadata, required values,
    # date coverage, and uniqueness before any candidate comparison is allowed.
    for route in ROUTES:
        daily = {}
        places = {}
        for page in source['routes'][route]['pages']:
            for row in page['response']['data']:
                day = parse_date(row['period'])
                key = tuple(row[field] for field in IDENTIFIERS[route])
                daily.setdefault(day, {})[key] = row
                identity = {'route': route, 'outage_date': day,
                            **{field: row[field] for field in IDENTIFIERS[route]}}
                if IDENTIFIERS[route] and row.get('facilityName') in (None, ''):
                    result['missing_labels'].append({**identity, 'facilityName': row.get('facilityName')})
                capacity, outage, percent = (numeric(row[field], field) for field in ('capacity', 'outage', 'percentOutage'))
                digits = max(0, -Decimal(str(row['percentOutage'])).as_tuple().exponent)
                places[str(digits)] = places.get(str(digits), 0) + 1
                values = {'capacity_mw': row['capacity'], 'outage_mw': row['outage'],
                          'reported_percent_outage': row['percentOutage']}
                if capacity <= 0:
                    result['undefined_ratios'].append({**identity, **values})
                else:
                    with localcontext() as context:
                        context.prec = 60
                        # Cross multiplication makes the boundary decision exact;
                        # the repeating ratio is calculated only for display.
                        outside = abs(percent * capacity - outage * 100) > Decimal('0.005') * capacity
                        ratio = outage / capacity * 100
                        comparison = {**identity, **values, 'calculated_percent_outage': ratio,
                                      'gap_percentage_points': percent - ratio}
                        previous_max = result['maximum_percentage_gaps'].get(route)
                        if previous_max is None or abs(percent - ratio) > abs(previous_max['gap_percentage_points']):
                            result['maximum_percentage_gaps'][route] = comparison
                        if outside:
                            result['percentage_candidates'].append({**comparison,
                                'classification': 'review_rounding_assumption_and_source_definition'})
        observations[route] = daily
        result['source_percentage_decimal_places'][route] = places
        days = sorted(daily)
        for before, after in zip(days, days[1:]):
            if (after - before).days != 1:
                result['skipped_intervals'].append({'route': route, 'previous_date': before, 'outage_date': after})
                continue
            old, new = daily[before], daily[after]
            old_keys, new_keys = set(old), set(new)
            added, removed = sorted(new_keys - old_keys), sorted(old_keys - new_keys)
            common = sorted(old_keys & new_keys)
            if added or removed:
                result['membership_changes'].append({'route': route, 'previous_date': before, 'outage_date': after,
                                                     'added_keys': added, 'removed_keys': removed})
            with localcontext() as context:
                context.prec = 60
                changed = []
                for key in common:
                    previous, current = old[key], new[key]
                    identity = {field: current[field] for field in IDENTIFIERS[route]}
                    delta = Decimal(str(current['capacity'])) - Decimal(str(previous['capacity']))
                    if delta:
                        change = {'route': route, 'previous_date': before, 'outage_date': after, **identity,
                                  'previous_capacity_mw': previous['capacity'], 'capacity_mw': current['capacity'],
                                  'change_mw': delta, 'classification': 'reported_capacity_change_not_proven_defect'}
                        result['capacity_changes'].append(change)
                        changed.append(delta)
                    if previous.get('facilityName') != current.get('facilityName'):
                        result['label_changes'].append({'route': route, 'previous_date': before, 'outage_date': after,
                            **identity, 'previous_name': previous.get('facilityName'), 'name': current.get('facilityName')})
                if changed or added or removed:
                    previous_total = sum((Decimal(str(row['capacity'])) for row in old.values()), Decimal(0))
                    total = sum((Decimal(str(row['capacity'])) for row in new.values()), Decimal(0))
                    added_mw = sum((Decimal(str(new[k]['capacity'])) for k in added), Decimal(0))
                    removed_mw = sum((Decimal(str(old[k]['capacity'])) for k in removed), Decimal(0))
                    result['capacity_decomposition'].append({'route': route, 'previous_date': before, 'outage_date': after,
                        'previous_capacity_mw': previous_total, 'capacity_mw': total, 'total_change_mw': total - previous_total,
                        'stable_key_count': len(common), 'changed_stable_keys': len(changed),
                        'stable_key_change_mw': sum(changed, Decimal(0)),
                        'added_key_count': len(added), 'added_capacity_mw': added_mw,
                        'removed_key_count': len(removed), 'removed_capacity_mw': removed_mw})
    facilities = observations['facility-nuclear-outages']
    for day, generators in observations['generator-nuclear-outages'].items():
        for (facility, generator), row in sorted(generators.items()):
            parent = facilities.get(day, {}).get((facility,))
            if parent is not None and parent.get('facilityName') != row.get('facilityName'):
                result['cross_grain_label_mismatches'].append({'outage_date': day, 'facility': facility,
                    'generator': generator, 'facility_name': parent.get('facilityName'), 'generator_facility_name': row.get('facilityName')})
    daily_changes = []
    national = observations['us-nuclear-outages']
    generators = observations['generator-nuclear-outages']
    with localcontext() as context:
        context.prec = 60
        for before, after in zip(sorted(national), sorted(national)[1:]):
            if (after - before).days != 1:
                continue
            old, new = national[before][()], national[after][()]
            common = generators.get(before, {}).keys() & generators.get(after, {}).keys()
            contributions = []
            for facility, generator in sorted(common):
                a, b = generators[before][facility, generator], generators[after][facility, generator]
                delta = Decimal(b['outage']) - Decimal(a['outage'])
                if delta:
                    contributions.append({'facility': facility, 'generator': generator,
                        'previous_outage_mw': a['outage'], 'outage_mw': b['outage'], 'change_mw': delta})
            delta = Decimal(new['outage']) - Decimal(old['outage'])
            daily_changes.append({'previous_date': before, 'outage_date': after,
                'previous_outage_mw': old['outage'], 'outage_mw': new['outage'],
                'change_mw': delta, 'absolute_change_mw': abs(delta),
                'generator_membership_unchanged': generators.get(before, {}).keys() == generators.get(after, {}).keys(),
                'generator_change_sum_mw': sum((r['change_mw'] for r in contributions), Decimal(0)),
                'generator_contributions': contributions})
        screening = tukey_summary([r['absolute_change_mw'] for r in daily_changes])
        for row in daily_changes:
            row['above_upper_fence'] = row['absolute_change_mw'] > screening['upper_fence']
    result['national_daily_outage_changes'] = daily_changes
    result['outage_change_screening'] = {**screening,
        'method': 'absolute adjacent national MW changes; type-7 linear quartiles; strictly above Q3 + 1.5 IQR',
        'classification': 'exploratory_operational_outlier_not_source_defect'}
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    fetch = sub.add_parser('fetch')
    fetch.add_argument('--output', type=Path, required=True, help='Fresh non-production .json.gz evidence file')
    fetch.add_argument('--end', type=parse_date)
    fetch.add_argument('--resume-from', type=Path, help='Resume verified partial evidence into a fresh output file')
    for command in ('analyse', 'inspect'):
        replay = sub.add_parser(command)
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
        result = inspect_temporal(source) if args.command == 'inspect' else analyse(source)
        analysis_destination(args.output)
        if args.input.resolve() == args.output.resolve():
            raise ExtractionError('Analysis output must not overwrite source evidence.')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, default=str, indent=2, allow_nan=False) + '\n')
        if args.command == 'inspect':
            print(encoded({'classification': result['classification'],
                           'capacity_decomposition': result['capacity_decomposition'],
                           'candidate_counts': {name: len(result[name]) for name in ('membership_changes', 'percentage_candidates', 'label_changes', 'cross_grain_label_mismatches', 'missing_labels')}}))
        else:
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
