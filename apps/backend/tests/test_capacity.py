"""Capacity metrics over synthetic, explicitly published Parquet snapshots."""

from datetime import date, timedelta
from decimal import Decimal
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from outage_explorer.analytics import AnalyticsError, open_analytics
from outage_explorer.capacity import daily_fleet_offline_share, offline_capacity
from outage_explorer.extract_eia import IDENTIFIERS, MEASURES, schema_for

START = date(2026, 1, 1)
END = START + timedelta(days=1)


def publish(root, *, capacity='100.123456', outage='25.000001'):
    directory = root / 'runs' / ('a' * 32)
    directory.mkdir(parents=True)
    manifest = {'schema_version': 1, 'start': str(START), 'end': str(END), 'routes': {}}
    for route, ids in IDENTIFIERS.items():
        identities = [{}]
        if ids:
            identities = [{'facility': '001', 'facilityName': None},
                          {'facility': '002', 'facilityName': 'Second'}]
        if 'generator' in ids:
            identities = [{**identities[0], 'generator': '01A'},
                          {**identities[0], 'generator': '02'},
                          {**identities[1], 'generator': '01A'}]
        rows = []
        for day in (START, END):
            for identity in identities:
                row = {'period': day, **identity, 'capacity': Decimal(capacity),
                       'outage': Decimal(outage), 'percentOutage': Decimal('24.98')}
                row.update({f'{measure}-units': units for measure, units in MEASURES.items()})
                rows.append(row)
        pq.write_table(pa.Table.from_pylist(rows, schema=schema_for(route)), directory / f'{route}.parquet')
        manifest['routes'][route] = {'file': f'{route}.parquet', 'records': len(rows),
                                    'candidate_key': ['period', *ids]}
    (directory / 'manifest.json').write_text(json.dumps(manifest))
    (root / 'current.json').write_text(json.dumps({'run': 'runs/' + 'a' * 32}))
    return directory


def test_exact_metrics_keep_source_grain_units_and_snapshot_unchanged(tmp_path):
    publish(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with open_analytics(tmp_path) as db:
        national = offline_capacity(db, START, END)
        assert len(national) == 2
        row = national[0]
        assert row == {'level': 'national', 'outage_date': START, 'facility_id': None, 'generator_id': None,
                       'capacity_mw': Decimal('100.123456'), 'outage_mw': Decimal('25.000001'),
                       'status': 'ok', 'offline_capacity_mw': Decimal('25.000001'),
                       'available_capacity_mw': Decimal('75.123455')}
        facilities = offline_capacity(db, START, END, level='facility')
        generators = offline_capacity(db, START, END, level='generator')
        assert len(facilities) == 4 and len(generators) == 6
        assert [(r['facility_id'], r['generator_id']) for r in generators[:3]] == [
            ('001', '01A'), ('001', '02'), ('002', '01A')]
        assert all(r['offline_capacity_mw'] == Decimal('25.000001') for r in facilities + generators)
        # The two facility and three generator rows must not inflate the national value.
        assert national[0]['offline_capacity_mw'] == Decimal('25.000001')
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('capacity,outage,status,available', [
    ('100', '0', 'ok', '100'), ('100', '100', 'ok', '0'), ('0', '0', 'zero_capacity', '0'),
    ('0', '1', 'inconsistent_values', None), ('100', '101', 'inconsistent_values', None),
    ('-1', '0', 'unsupported_values', None), ('100', '-1', 'unsupported_values', None),
    ('999999999999999999.999999', '0.000001', 'ok', '999999999999999999.999998'),
])
def test_zero_full_outage_inconsistent_and_precision_edges(tmp_path, capacity, outage, status, available):
    publish(tmp_path, capacity=capacity, outage=outage)
    with open_analytics(tmp_path) as db:
        rows = offline_capacity(db, START, END)
    assert all(r['status'] == status for r in rows)
    for row in rows:
        assert row['capacity_mw'] == Decimal(capacity) and row['outage_mw'] == Decimal(outage)
        assert row['available_capacity_mw'] == (Decimal(available) if available is not None else None)
        assert row['offline_capacity_mw'] == (Decimal(outage) if available is not None else None)


@pytest.mark.parametrize('field', ['capacity', 'outage'])
def test_null_parquet_inputs_are_rejected_by_snapshot_opening(tmp_path, field):
    directory = publish(tmp_path)
    path = directory / 'us-nuclear-outages.parquet'
    table = pq.ParquetFile(path).read()
    i = table.schema.get_field_index(field)
    # A nullable physical field is itself incompatible with the publication schema.
    table = table.set_column(i, field, pa.array([None, Decimal('1')], type=pa.decimal128(24, 6)))
    pq.write_table(table, path)
    with pytest.raises(AnalyticsError, match='snapshot'):
        open_analytics(tmp_path)


@pytest.mark.parametrize('field,value,status', [
    ('capacity_mw', None, 'missing_values'), ('outage_mw', None, 'missing_values'),
    ('capacity_mw', float('nan'), 'unsupported_values'), ('outage_mw', float('inf'), 'unsupported_values'),
])
def test_defensive_missing_and_nonfinite_values_are_not_zero(tmp_path, field, value, status):
    publish(tmp_path)
    with open_analytics(tmp_path) as db:
        # Deliberately incompatible in-memory inputs exercise defensive branches;
        # production snapshot validation never accepts these source values.
        db.execute('CREATE OR REPLACE TEMP TABLE national_outages AS SELECT * FROM national_outages')
        if value is not None:
            db.execute(f'ALTER TABLE national_outages ALTER {field} TYPE DOUBLE')
        db.execute(f'UPDATE national_outages SET {field} = ? WHERE outage_date = ?', [value, START])
        rows = offline_capacity(db, START, END)
        assert rows[0]['status'] == status
        assert rows[0]['offline_capacity_mw'] is None and rows[0]['available_capacity_mw'] is None
        assert rows[1]['status'] == 'ok'


def test_missing_dates_are_explicit_and_duplicate_keys_are_not_aggregated(tmp_path):
    publish(tmp_path)
    with open_analytics(tmp_path) as db:
        db.execute('CREATE OR REPLACE TEMP TABLE national_outages AS SELECT * FROM national_outages')
        db.execute('DELETE FROM national_outages WHERE outage_date = ?', [END])
        db.execute('INSERT INTO national_outages SELECT * FROM national_outages')
        rows = offline_capacity(db, START, END)
        assert [r['status'] for r in rows] == ['duplicate_key', 'duplicate_key', 'missing_date']
        assert all(r['offline_capacity_mw'] is None and r['available_capacity_mw'] is None for r in rows)
        assert rows[-1]['outage_date'] == END and rows[-1]['capacity_mw'] is None


def test_metric_does_not_use_rounded_source_percent_or_reopen_pointer(tmp_path):
    publish(tmp_path)
    with open_analytics(tmp_path) as db:
        (tmp_path / 'current.json').write_text('{not-a-new-publication')
        rows = offline_capacity(db, START, END)
        # Neither 100 * 24.98% nor capacity * the rounded source percentage.
        assert rows[0]['offline_capacity_mw'] == Decimal('25.000001')
        assert rows[0]['available_capacity_mw'] == Decimal('75.123455')


def test_empty_route_has_daily_missing_results_without_inventing_entities(tmp_path):
    publish(tmp_path)
    with open_analytics(tmp_path) as db:
        db.execute('CREATE OR REPLACE TEMP TABLE generator_outages AS SELECT * FROM generator_outages WHERE false')
        rows = offline_capacity(db, START, END, level='generator')
        assert len(rows) == 2 and all(r['status'] == 'missing_date' for r in rows)
        assert all(r['facility_id'] is None and r['generator_id'] is None for r in rows)


def test_invalid_arguments_and_missing_snapshot(tmp_path):
    with pytest.raises(AnalyticsError, match='No published snapshot'):
        open_analytics(tmp_path)
    publish(tmp_path)
    with open_analytics(tmp_path) as db:
        with pytest.raises(ValueError, match='level'):
            offline_capacity(db, START, END, level='national_outages; DROP TABLE anything')
        with pytest.raises(ValueError, match='date window'):
            offline_capacity(db, END, START)
        with pytest.raises(ValueError, match='date window'):
            offline_capacity(db, '2026-01-01', END)


def test_us08_formula_multiple_dates_and_national_source_only(tmp_path):
    directory = publish(tmp_path, capacity='300', outage='100')
    path = directory / 'us-nuclear-outages.parquet'
    table = pq.ParquetFile(path).read()
    rows = table.to_pylist()
    rows[1]['capacity'] = Decimal('200')
    rows[1]['outage'] = Decimal('50')
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
    with open_analytics(tmp_path) as db:
        # Even removing both other levels from the catalog cannot change US-08.
        db.execute('DROP VIEW facility_outages')
        db.execute('DROP VIEW generator_outages')
        result = daily_fleet_offline_share(db, START, END)
    assert result == [
        {'outage_date': START, 'total_fleet_capacity_mw': Decimal('300'),
         'offline_capacity_mw': Decimal('100'), 'fleet_offline_share_pct': Decimal('33.333333'), 'status': 'ok'},
        {'outage_date': END, 'total_fleet_capacity_mw': Decimal('200'),
         'offline_capacity_mw': Decimal('50'), 'fleet_offline_share_pct': Decimal('25.000000'), 'status': 'ok'}]


@pytest.mark.parametrize('capacity,outage,expected,status', [
    ('100', '0', '0.000000', 'ok'), ('100', '100', '100.000000', 'ok'),
    ('0', '0', None, 'zero_capacity'), ('0', '1', None, 'inconsistent_values'),
    ('-1', '0', None, 'unsupported_values'), ('100', '-1', None, 'unsupported_values'),
    ('100', '101', None, 'inconsistent_values'),
    ('999999999999999999.999999', '999999999999999999.999998', '100.000000', 'ok'),
    ('200', '0.000001', '0.000000', 'ok'),  # Exact half-even tie.
    ('200', '0.000003', '0.000002', 'ok'),
])
def test_us08_zero_invalid_precision_and_rounding(tmp_path, capacity, outage, expected, status):
    publish(tmp_path, capacity=capacity, outage=outage)
    with open_analytics(tmp_path) as db:
        result = daily_fleet_offline_share(db, START, END)
    assert len(result) == 2
    assert all(r['status'] == status for r in result)
    assert all(r['fleet_offline_share_pct'] == (Decimal(expected) if expected is not None else None) for r in result)
    if status == 'zero_capacity':
        assert result[0]['total_fleet_capacity_mw'] == result[0]['offline_capacity_mw'] == Decimal(0)


@pytest.mark.parametrize('field,value,status', [
    ('capacity_mw', None, 'missing_values'), ('outage_mw', None, 'missing_values'),
    ('capacity_mw', float('nan'), 'unsupported_values'), ('outage_mw', float('inf'), 'unsupported_values'),
])
def test_us08_defensive_invalid_values(tmp_path, field, value, status):
    publish(tmp_path)
    with open_analytics(tmp_path) as db:
        db.execute('CREATE OR REPLACE TEMP TABLE national_outages AS SELECT * FROM national_outages')
        if value is not None:
            db.execute(f'ALTER TABLE national_outages ALTER {field} TYPE DOUBLE')
        db.execute(f'UPDATE national_outages SET {field} = ? WHERE outage_date = ?', [value, START])
        # Keep both days invalid: incompatible DOUBLE tables are defensive test
        # inputs, not supported physical schemas for calculating a valid ratio.
        db.execute(f'UPDATE national_outages SET {field} = ?', [value])
        result = daily_fleet_offline_share(db, START, END)
    assert all(r['status'] == status and r['fleet_offline_share_pct'] is None for r in result)
    if status == 'unsupported_values':
        assert all(r['total_fleet_capacity_mw'] is None for r in result)


def test_us08_duplicate_and_missing_days_return_one_row_each(tmp_path):
    publish(tmp_path)
    with open_analytics(tmp_path) as db:
        db.execute('CREATE OR REPLACE TEMP TABLE national_outages AS SELECT * FROM national_outages')
        db.execute('DELETE FROM national_outages WHERE outage_date = ?', [END])
        db.execute('INSERT INTO national_outages SELECT * FROM national_outages')
        result = daily_fleet_offline_share(db, START, END)
    assert [r['outage_date'] for r in result] == [START, END]
    assert [r['status'] for r in result] == ['duplicate_key', 'missing_date']
    assert all(r['fleet_offline_share_pct'] is None and r['total_fleet_capacity_mw'] is None
               and r['offline_capacity_mw'] is None for r in result)


def test_us08_ignores_rounded_source_percentage_and_stays_pinned(tmp_path):
    publish(tmp_path, capacity='100056.7', outage='19859.767')
    with open_analytics(tmp_path) as db:
        (tmp_path / 'current.json').write_text('{broken-new-pointer')
        result = daily_fleet_offline_share(db, START, END)
    assert result[0]['fleet_offline_share_pct'] == Decimal('19.848513')
