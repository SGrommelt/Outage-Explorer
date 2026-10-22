"""Pinned real EIA demonstration data, never a production publication.

Only the checked-in archive is accepted. Demo artifacts have no current pointer,
production manifest, watermarks, or ingestion audit and cannot be opened by the
production reader. Source completeness remains explicitly unverified.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from outage_explorer.analytics import AnalyticsError, VIEWS
from outage_explorer.analysis_eia import analyse
from outage_explorer.explore_eia import ROUTES
from outage_explorer.extract_eia import parse_date, schema_for, validate_record, write_json, sync_directory
from outage_explorer.refresh_eia import validated_rows

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = ROOT / 'docs/evidence/eia-real-30-days-source.json.gz'
SUMMARY = ROOT / 'docs/evidence/eia-real-30-days-summary.json'
DEFAULT_DEMO_ROOT = ROOT / 'data/eia-demo'
NOTICE = 'DEMO: saved real EIA observations; source completeness is unverified. Read-only; no production publication.'
PURPOSE = 'DEMO_ONLY_COMPLETENESS_UNVERIFIED'


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def destination(root: Path) -> Path:
    root = Path(root).resolve()
    production = ROOT / 'data/eia/incremental'
    if root.name != 'eia-demo' or root.is_relative_to(production) or production.is_relative_to(root):
        raise AnalyticsError('Demo data requires a separate eia-demo directory.')
    return root


def pinned_source() -> tuple[dict, str]:
    expected = json.loads(SUMMARY.read_text())['source_file_sha256']
    if sha(EVIDENCE) != expected:
        raise AnalyticsError('Demo evidence checksum does not match the pinned archive.')
    return json.loads(gzip.decompress(EVIDENCE.read_bytes())), expected


def _verify(directory: Path, expected: str) -> dict:
    manifest = json.loads((directory / 'demo.json').read_text())
    if (manifest.get('purpose') != PURPOSE or manifest.get('production_eligible') is not False
            or manifest.get('source_sha256') != expected or set(manifest.get('routes', {})) != set(ROUTES)):
        raise AnalyticsError('Invalid demo provenance metadata.')
    summary = json.loads(SUMMARY.read_text())
    if manifest.get('start') != summary['start'] or manifest.get('end') != summary['end'] or manifest.get('notice') != NOTICE:
        raise AnalyticsError('Demo coverage or caveat metadata changed.')
    start, end = parse_date(manifest['start']), parse_date(manifest['end'])
    for route in ROUTES:
        entry = manifest['routes'][route]
        if {key: value for key, value in entry.items() if key != 'sha256'} != summary['routes'][route]:
            raise AnalyticsError('Demo source count/completeness metadata changed.')
        path = directory / f'{route}.parquet'
        if sha(path) != entry['sha256']:
            raise AnalyticsError('Demo Parquet checksum mismatch.')
        rows = validated_rows(path, route, start, end)
        if len(rows) != entry['validated_rows']:
            raise AnalyticsError('Demo row count mismatch.')
    return manifest


def build_demo(root: Path = DEFAULT_DEMO_ROOT) -> Path:
    """Materialize once into a content-addressed, immutable demonstration directory."""
    root = destination(root)
    source, expected = pinned_source()
    report = analyse(source)
    if (report.get('requested_days') != 30 or len(report['daily']) != 30
            or any(row['status'] != 'equal' for row in report['daily'] + report['facilities'])):
        raise AnalyticsError('Demo evidence failed cross-grain reconciliation.')
    for route in ROUTES:
        entry = report['routes'][route]
        if (not entry['valid_records'] or entry['days_covered'] != 30 or not entry['terminal_empty_page']
                or entry['received_rows'] != entry['unique_keys'] or len(set(entry['reported_totals'])) != 1
                or source['routes'][route]['failures']):
            raise AnalyticsError('Demo evidence failed traversal, schema, key, or coverage validation.')
        if route != 'facility-nuclear-outages' and not entry['count_agrees']:
            raise AnalyticsError('National/generator demo count validation failed.')
    if any(report['anomalies'][name] for name in ('missing_dates', 'invalid_records', 'duplicate_keys', 'range_flags')):
        raise AnalyticsError('Demo evidence failed quality checks.')
    directory = root / 'datasets' / expected
    if directory.exists():
        _verify(directory, expected)
        return directory
    directory.parent.mkdir(parents=True, exist_ok=True)
    start, end = parse_date(report['start']), parse_date(report['end'])
    with tempfile.TemporaryDirectory(prefix='.building-', dir=root) as temporary:
        stage = Path(temporary)
        manifest = {'purpose': PURPOSE, 'production_eligible': False, 'notice': NOTICE,
                    'source_sha256': expected, 'start': str(start), 'end': str(end), 'routes': {},
                    'validation_basis': 'Pinned evidence; exact schemas/keys/dates; terminal traversal; national/generator counts; exact cross-grain key and MW agreement. Not independent source-coverage certification.'}
        for route in ROUTES:
            rows = [validate_record(row, route, start, end)[0]
                    for page in source['routes'][route]['pages'] for row in page['response']['data']]
            path = stage / f'{route}.parquet'
            pq.write_table(pa.Table.from_pylist(rows, schema=schema_for(route)), path, compression='zstd')
            with path.open('rb') as file:
                os.fsync(file.fileno())
            entry = report['routes'][route]
            manifest['routes'][route] = {**entry, 'sha256': sha(path)}
        write_json(stage / 'demo.json', manifest)
        _verify(stage, expected)
        sync_directory(stage)
        # Never replace an existing directory. No current.json is created here.
        os.rename(stage, directory)
        sync_directory(directory.parent)
    return directory


def open_demo(root: Path = DEFAULT_DEMO_ROOT) -> duckdb.DuckDBPyConnection:
    """Open only the pinned demo, never fall back to or repair production data."""
    connection = None
    try:
        root = destination(root)
        _, expected = pinned_source()
        directory = root / 'datasets' / expected
        _verify(directory, expected)
        connection = duckdb.connect(':memory:')
        for name, (route, identifiers) in VIEWS.items():
            connection.read_parquet(str(directory / f'{route}.parquet'), hive_partitioning=False).project(
                f'period AS outage_date, {identifiers} capacity AS capacity_mw, outage AS outage_mw, percentOutage AS percent_outage'
            ).create_view(name)
        return connection
    except BaseException as exc:
        if connection is not None:
            connection.close()
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise AnalyticsError('Demo data is missing, incompatible, or corrupt; rebuild in a fresh demo directory.') from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        directory = build_demo()
        metadata = json.loads((directory / 'demo.json').read_text())
        print(NOTICE)
        print(json.dumps({'start': metadata['start'], 'end': metadata['end'],
                          'validated_rows': {route: entry['validated_rows'] for route, entry in metadata['routes'].items()}}))
        return 0
    except Exception:
        print('Demo preparation failed validation; production data was not touched. Details withheld.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
