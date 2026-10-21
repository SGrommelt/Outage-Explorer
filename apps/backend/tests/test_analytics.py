"""Offline analytical model tests using small synthetic published Parquet runs."""

from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import shutil

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from outage_explorer import analytics
from outage_explorer.extract_eia import IDENTIFIERS, MEASURES, schema_for

START = date(2026, 10, 18)
END = START + timedelta(days=1)


def publish(root: Path, version='a' * 32, *, schema_version=2, outage='12.123456'):
    directory = root / 'runs' / version
    directory.mkdir(parents=True)
    manifest = {'schema_version': schema_version, 'snapshot_version': version,
                'start': str(START), 'end': str(END), 'routes': {},
                'watermarks': {route: str(END) for route in IDENTIFIERS}}
    for route, identifiers in IDENTIFIERS.items():
        identities = [{}]
        if identifiers:
            identities = [{'facility': '003', 'facilityName': None},
                          {'facility': '77', 'facilityName': 'Other facility'}]
        if 'generator' in identifiers:
            identities = [{**identities[0], 'generator': '001A'},
                          {**identities[0], 'generator': '02'},
                          {**identities[1], 'generator': '001A'}]
        rows = []
        for day in (START, END):
            for identity in identities:
                row = {'period': day, **identity, 'capacity': Decimal('123456789012345678.123456'),
                       'outage': Decimal(outage), 'percentOutage': Decimal('2.123456')}
                row.update({f'{measure}-units': units for measure, units in MEASURES.items()})
                rows.append(row)
        pq.write_table(pa.Table.from_pylist(rows, schema=schema_for(route)), directory / f'{route}.parquet')
        manifest['routes'][route] = {'file': f'{route}.parquet', 'records': len(rows),
                                     'candidate_key': ['period', *identifiers]}
    manifest['ingestion_run'] = {
        'run_id': version, 'status': 'published', 'published_snapshot_version': version,
        'requested_end': str(END), 'finished_at_utc': '2026-10-20T00:00:00+00:00', 'failures': [],
        'routes': {route: {'status': 'published', 'watermark_after': str(END),
                           'merged_rows': entry['records']} for route, entry in manifest['routes'].items()}}
    (directory / 'manifest.json').write_text(json.dumps(manifest))
    (root / 'current.json').write_text(json.dumps({'run': f'runs/{version}'}))
    return directory


@pytest.mark.parametrize('schema_version', [1, 2])
def test_views_preserve_grain_values_and_types_without_copying(tmp_path, schema_version):
    publish(tmp_path, schema_version=schema_version)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with analytics.open_analytics(tmp_path) as connection:
        assert connection.execute('SHOW TABLES').fetchall() == [
            ('facility_outages',), ('generator_outages',), ('national_outages',)]
        for view, count in [('national_outages', 2), ('facility_outages', 4), ('generator_outages', 6)]:
            assert connection.execute(f'SELECT count(*) FROM {view}').fetchone() == (count,)
            columns = {row[0]: row[1] for row in connection.execute(f'DESCRIBE {view}').fetchall()}
            assert columns['outage_date'] == 'DATE'
            for measure in ['capacity_mw', 'outage_mw', 'percent_outage']:
                assert columns[measure] == 'DECIMAL(24,6)'
            if view != 'national_outages':
                assert columns['facility_id'] == columns['facility_name'] == 'VARCHAR'
            if view == 'generator_outages':
                assert columns['generator_id'] == 'VARCHAR'
        assert connection.execute('SELECT * FROM national_outages ORDER BY outage_date LIMIT 1').fetchone() == (
            START, Decimal('123456789012345678.123456'), Decimal('12.123456'), Decimal('2.123456'))
        assert connection.execute("SELECT facility_id, generator_id, facility_name FROM generator_outages "
                                  "WHERE outage_date = ? ORDER BY facility_id, generator_id", [START]).fetchall() == [
                                      ('003', '001A', None), ('003', '02', None), ('77', '001A', 'Other facility')]
        assert connection.execute("SELECT count(*) FROM duckdb_tables() WHERE NOT internal").fetchone() == (0,)
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert after == before


def test_queries_preserve_grains_and_aggregate_explicitly(tmp_path):
    publish(tmp_path)
    with analytics.open_analytics(tmp_path) as connection:
        assert connection.execute('SELECT outage_date, outage_mw FROM national_outages ORDER BY outage_date').fetchall() == [
            (START, Decimal('12.123456')), (END, Decimal('12.123456'))]
        assert connection.execute('SELECT facility_id, sum(outage_mw) FROM facility_outages '
                                  'GROUP BY facility_id ORDER BY facility_id').fetchall() == [
                                      ('003', Decimal('24.246912')), ('77', Decimal('24.246912'))]
        assert connection.execute("SELECT generator_id, outage_mw FROM generator_outages "
                                  "WHERE facility_id = '003' AND outage_date = ? ORDER BY generator_id", [START]).fetchall() == [
                                      ('001A', Decimal('12.123456')), ('02', Decimal('12.123456'))]
        # Aggregate children to the parent key before comparing; never sum the
        # repeated parent outage after joining daily facilities to generators.
        assert connection.execute('WITH g AS (SELECT outage_date, facility_id, count(*) AS generators '
                                  'FROM generator_outages GROUP BY outage_date, facility_id) '
                                  'SELECT count(*), sum(g.generators) FROM facility_outages f '
                                  'LEFT JOIN g USING (outage_date, facility_id)').fetchone() == (4, 6)


def test_connection_resolves_pointer_once_and_remains_pinned(tmp_path, monkeypatch):
    publish(tmp_path)
    publish(tmp_path, 'b' * 32, outage='99')
    pointer = tmp_path / 'current.json'
    old_pointer = json.dumps({'run': 'runs/' + 'a' * 32})
    new_pointer = pointer.read_text()
    pointer.write_text(old_pointer)
    load = analytics.load_snapshot
    calls = []
    def switch_after_validation(root):
        calls.append(root)
        result = load(root)
        pointer.write_text(new_pointer)
        return result
    monkeypatch.setattr(analytics, 'load_snapshot', switch_after_validation)
    with analytics.open_analytics(tmp_path) as pinned:
        assert len(calls) == 1
        for view in analytics.VIEWS:
            assert pinned.execute(f'SELECT DISTINCT outage_mw FROM {view}').fetchall() == [(Decimal('12.123456'),)]
        with analytics.open_analytics(tmp_path) as refreshed:
            for view in analytics.VIEWS:
                assert refreshed.execute(f'SELECT DISTINCT outage_mw FROM {view}').fetchall() == [(Decimal('99'),)]
        assert pinned.execute('SELECT DISTINCT outage_mw FROM national_outages').fetchall() == [(Decimal('12.123456'),)]


def test_staging_and_unpublished_runs_are_never_scanned(tmp_path):
    old = publish(tmp_path)
    pointer = (tmp_path / 'current.json').read_bytes()
    publish(tmp_path, 'b' * 32, outage='99')
    shutil.copytree(old, tmp_path / '.staging-unpublished')
    (tmp_path / 'current.json').write_bytes(pointer)
    with analytics.open_analytics(tmp_path) as connection:
        assert connection.execute('SELECT count(*), max(outage_mw) FROM generator_outages').fetchone() == (6, Decimal('12.123456'))
    (tmp_path / 'current.json').unlink()
    with pytest.raises(analytics.AnalyticsError, match='No published snapshot'):
        analytics.open_analytics(tmp_path)


@pytest.mark.parametrize('fault', ['missing_root', 'missing_file', 'invalid_pointer', 'malformed_manifest',
                                  'schema', 'row_count', 'watermark', 'audit', 'duplicate_key'])
def test_invalid_published_snapshots_fail_clearly(tmp_path, fault):
    root = tmp_path / 'snapshot'
    if fault == 'missing_root':
        with pytest.raises(analytics.AnalyticsError, match='No published snapshot'):
            analytics.open_analytics(root)
        assert not root.exists()
        return
    directory = publish(root)
    file = directory / 'us-nuclear-outages.parquet'
    manifest_path = directory / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if fault == 'missing_file':
        file.unlink()
    elif fault == 'invalid_pointer':
        (root / 'current.json').write_text(json.dumps({'run': '.staging-unpublished'}))
    elif fault == 'malformed_manifest':
        manifest_path.write_text('{')
    elif fault in ('schema', 'duplicate_key'):
        table = pq.ParquetFile(file).read()
        if fault == 'schema':
            i = table.schema.get_field_index('outage')
            table = table.set_column(i, 'outage', pa.array([1.0, 2.0]))
        else:
            table = pa.concat_tables([table, table.slice(0, 1)])
        pq.write_table(table, file)
    else:
        if fault == 'row_count':
            manifest['routes']['us-nuclear-outages']['records'] = 999
        elif fault == 'watermark':
            manifest['watermarks']['us-nuclear-outages'] = str(START)
        else:
            manifest['ingestion_run']['status'] = 'interrupted'
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(analytics.AnalyticsError, match='snapshot'):
        analytics.open_analytics(root)


def test_hive_style_and_quoted_directory_names(tmp_path):
    root = tmp_path / 'source=eia' / "reader's snapshot"
    publish(root)
    with analytics.open_analytics(root) as connection:
        assert [column[0] for column in connection.execute('DESCRIBE national_outages').fetchall()] == [
            'outage_date', 'capacity_mw', 'outage_mw', 'percent_outage']
        assert connection.execute('SELECT count(*) FROM generator_outages').fetchone() == (6,)


def test_duckdb_binding_error_closes_connection(tmp_path, monkeypatch):
    publish(tmp_path)
    connect = duckdb.connect
    opened = []
    def capture(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection
    monkeypatch.setattr(analytics.duckdb, 'connect', capture)
    monkeypatch.setattr(analytics, 'VIEWS', {'bad': ('absent', '')})
    with pytest.raises(analytics.AnalyticsError, match='Unable to create analytical views'):
        analytics.open_analytics(tmp_path)
    with pytest.raises(duckdb.ConnectionException):
        opened[0].execute('SELECT 1')
