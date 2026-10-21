"""Synthetic, offline diagnostics, including faults ingestion would normally reject."""

from datetime import date, timedelta
from decimal import Decimal
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from outage_explorer.analytics import open_analytics
from outage_explorer import diagnostics as checks
from outage_explorer.extract_eia import IDENTIFIERS, MEASURES, schema_for

START = date(2026, 1, 1)
END = START + timedelta(days=3)


@pytest.fixture
def snapshot(tmp_path):
    directory = tmp_path / 'runs' / ('a' * 32)
    directory.mkdir(parents=True)
    manifest = {'schema_version': 1, 'start': str(START), 'end': str(END), 'routes': {}}
    for route, ids in IDENTIFIERS.items():
        identities = [({}, 300, 30)]
        if ids:
            identities = [({'facility': '001'}, 200, 20), ({'facility': '002'}, 100, 10)]
        if 'generator' in ids:
            identities = [({'facility': '001', 'generator': '01'}, 100, 10),
                          ({'facility': '001', 'generator': '02'}, 100, 10),
                          ({'facility': '002', 'generator': '01'}, 100, 10)]
        rows = []
        for offset in range(4):
            for identity, capacity, outage in identities:
                row = {'period': START + timedelta(days=offset), **identity,
                       'capacity': Decimal(capacity), 'outage': Decimal(outage), 'percentOutage': Decimal(10)}
                if ids:
                    row['facilityName'] = None
                row.update({f'{measure}-units': units for measure, units in MEASURES.items()})
                rows.append(row)
        pq.write_table(pa.Table.from_pylist(rows, schema=schema_for(route)), directory / f'{route}.parquet')
        manifest['routes'][route] = {'file': f'{route}.parquet', 'records': len(rows),
                                    'candidate_key': ['period', *ids]}
    (directory / 'manifest.json').write_text(json.dumps(manifest))
    (tmp_path / 'current.json').write_text(json.dumps({'run': 'runs/' + 'a' * 32}))
    return tmp_path


@pytest.fixture
def db(snapshot):
    with open_analytics(snapshot) as connection:
        yield connection


def editable(db, view):
    # Alter isolated in-memory copies, never the immutable published Parquet.
    db.execute(f'CREATE OR REPLACE TEMP TABLE {view} AS SELECT * FROM {view}')


def test_normal_reconciliation_and_anomalies_are_read_only(db, snapshot):
    before = {p: p.read_bytes() for p in snapshot.rglob('*') if p.is_file()}
    daily = checks.reconcile_daily(db, START, END)
    assert len(daily) == 4
    assert all(r['status'] == 'equal' and r['category'] == 'source_comparison' for r in daily)
    assert daily[0]['facility_count'] == 2 and daily[0]['generator_count'] == 3
    assert daily[0]['facility_minus_national_mw'] == Decimal('0.000000')
    facilities = checks.reconcile_facilities(db, START, END)
    assert len(facilities) == 8 and all(r['status'] == 'equal' for r in facilities)
    assert facilities[0]['facility_id'] == '001' and facilities[0]['generator_count'] == 2
    assert all(not rows for rows in checks.find_anomalies(db, START, END, change_threshold_mw=Decimal('1')).values())
    assert {p: p.read_bytes() for p in snapshot.rglob('*') if p.is_file()} == before


def test_small_exact_differences_are_comparisons_not_data_quality(db):
    editable(db, 'facility_outages')
    db.execute("UPDATE facility_outages SET outage_mw = outage_mw + 0.000001 WHERE facility_id = '001'")
    daily = checks.reconcile_daily(db, START, END)
    assert all(r['status'] == 'difference' and r['category'] == 'source_comparison' for r in daily)
    assert daily[0]['facility_minus_national_mw'] == Decimal('0.000001')
    assert daily[0]['generator_minus_national_mw'] == Decimal('0.000000')
    rows = checks.reconcile_facilities(db, START, END)
    assert rows[0]['generator_minus_facility_mw'] == Decimal('-0.000001')
    assert rows[1]['status'] == 'equal'
    assert all(not rows for rows in checks.find_anomalies(db, START, END).values())


def test_capacity_difference_is_visible_without_summing_percentages(db):
    editable(db, 'national_outages')
    db.execute('UPDATE national_outages SET capacity_mw = capacity_mw + 1, percent_outage = 9.999999')
    daily = checks.reconcile_daily(db, START, END)
    assert daily[0]['facility_capacity_minus_national_mw'] == Decimal('-1')
    assert daily[0]['status'] == 'difference'
    assert 'percent_outage' not in daily[0]


def test_missing_cross_level_groups_remain_null_and_unmatched_sides_survive(db):
    editable(db, 'facility_outages')
    editable(db, 'generator_outages')
    db.execute("DELETE FROM facility_outages WHERE facility_id = '001' AND outage_date = ?", [START])
    db.execute("DELETE FROM generator_outages WHERE facility_id = '002' AND outage_date = ?", [START])
    rows = checks.reconcile_facilities(db, START, START)
    assert len(rows) == 2 and all(r['status'] == 'missing_level' for r in rows)
    assert rows[0]['facility_outage_mw'] is None and rows[0]['generator_outage_mw'] == Decimal(20)
    assert rows[1]['generator_outage_mw'] is None and rows[1]['generator_minus_facility_mw'] is None
    # A missing entity is not assumed to be a lifecycle/data-quality violation.
    assert checks.find_anomalies(db, START, START)['missing_dates'] == []


def test_missing_route_dates_include_boundaries_and_empty_levels(db):
    editable(db, 'facility_outages')
    editable(db, 'national_outages')
    db.execute('DELETE FROM national_outages WHERE outage_date IN (?, ?)', [START, END])
    db.execute('DELETE FROM facility_outages')
    anomalies = checks.find_anomalies(db, START, END)
    assert len(anomalies['missing_dates']) == 6
    assert {'category': 'data_quality', 'level': 'national', 'outage_date': START} in anomalies['missing_dates']
    daily = checks.reconcile_daily(db, START, END)
    assert all(r['status'] == 'missing_level' and r['facility_outage_mw'] is None for r in daily)


def test_invalid_required_values_and_duplicate_keys_are_contract_failures(db):
    editable(db, 'national_outages')
    editable(db, 'facility_outages')
    editable(db, 'generator_outages')
    db.execute('UPDATE national_outages SET outage_mw = NULL WHERE outage_date = ?', [START])
    db.execute("UPDATE facility_outages SET facility_id = ' ' WHERE outage_date = ? AND facility_id = '001'", [START])
    db.execute("UPDATE generator_outages SET generator_id = NULL WHERE outage_date = ? AND facility_id = '002'", [START])
    db.execute('UPDATE national_outages SET outage_date = NULL WHERE outage_date = ?', [END])
    db.execute('INSERT INTO facility_outages SELECT * FROM facility_outages WHERE outage_date = ?', [END])
    anomalies = checks.find_anomalies(db, START, END)
    assert len(anomalies['invalid_records']) == 4
    assert all(r['category'] == 'data_quality' for r in anomalies['invalid_records'])
    assert len(anomalies['duplicate_keys']) == 2
    assert all(r['records'] == 2 for r in anomalies['duplicate_keys'])


def test_nonfinite_values_are_invalid_in_defensive_compatible_tables(db):
    editable(db, 'national_outages')
    db.execute('ALTER TABLE national_outages ALTER outage_mw TYPE DOUBLE')
    db.execute("UPDATE national_outages SET outage_mw = 'NaN'::DOUBLE WHERE outage_date = ?", [START])
    assert len(checks.find_anomalies(db, START, END)['invalid_records']) == 1


def test_unverified_ranges_are_review_flags_not_ingestion_errors(db):
    editable(db, 'facility_outages')
    db.execute("UPDATE facility_outages SET outage_mw = -1, percent_outage = -2 WHERE facility_id = '001'")
    db.execute("UPDATE facility_outages SET outage_mw = 200, percent_outage = 150 WHERE facility_id = '002'")
    result = checks.find_anomalies(db, START, END)
    assert len(result['range_flags']) == 8
    assert all(r['category'] == 'review' for r in result['range_flags'])
    assert result['range_flags'][0]['negative_outage']
    assert result['range_flags'][1]['outage_above_capacity']
    assert result['invalid_records'] == []


def test_change_detection_is_explicit_exact_scoped_and_includes_previous_day(db):
    editable(db, 'generator_outages')
    db.execute("UPDATE generator_outages SET outage_mw = 12.000001 WHERE outage_date >= ? "
               "AND facility_id = '001' AND generator_id = '01'", [START + timedelta(days=1)])
    assert checks.find_anomalies(db, START, END)['large_changes'] == []
    result = checks.find_anomalies(db, START + timedelta(days=1), END, change_threshold_mw=Decimal('2'))
    assert len(result['large_changes']) == 1
    row = result['large_changes'][0]
    assert row['facility_id'] == '001' and row['generator_id'] == '01'
    assert row['previous_date'] == START and row['change_mw'] == Decimal('2.000001')
    assert row['threshold_mw'] == Decimal('2') and row['category'] == 'review'
    assert checks.find_anomalies(db, START, END, change_threshold_mw=Decimal('2.000001'))['large_changes'] == []


def test_changes_do_not_bridge_gaps_or_duplicate_observations(db):
    editable(db, 'national_outages')
    db.execute('DELETE FROM national_outages WHERE outage_date = ?', [START + timedelta(days=1)])
    db.execute('UPDATE national_outages SET outage_mw = 80 WHERE outage_date >= ?', [START + timedelta(days=2)])
    assert checks.find_anomalies(db, START, END, change_threshold_mw=Decimal('1'))['large_changes'] == []
    db.execute('INSERT INTO national_outages SELECT * FROM national_outages WHERE outage_date = ?', [START + timedelta(days=2)])
    db.execute('UPDATE national_outages SET outage_mw = 200 WHERE outage_date = ?', [END])
    assert checks.find_anomalies(db, START, END, change_threshold_mw=Decimal('1'))['large_changes'] == []


@pytest.mark.parametrize('threshold', [Decimal('0'), Decimal('-1'), Decimal('NaN'), Decimal('Infinity'),
                                       Decimal('0.0000001'), Decimal('1e18'), 1, 1.5, '2'])
def test_invalid_change_thresholds_fail_explicitly(db, threshold):
    with pytest.raises(ValueError, match='change_threshold_mw'):
        checks.find_anomalies(db, START, END, change_threshold_mw=threshold)


@pytest.mark.parametrize('function', [checks.reconcile_daily, checks.reconcile_facilities, checks.find_anomalies])
def test_invalid_date_windows_fail_explicitly(db, function):
    with pytest.raises(ValueError, match='date window'):
        function(db, END, START)
    with pytest.raises(ValueError, match='date window'):
        function(db, '2026-01-01', END)


def test_diagnostics_keep_using_the_existing_pinned_snapshot(snapshot):
    with open_analytics(snapshot) as db:
        # A later publication/failure cannot redirect any diagnostic query.
        (snapshot / 'current.json').write_text('{broken-new-pointer')
        assert all(r['status'] == 'equal' for r in checks.reconcile_daily(db, START, END))
        assert all(not rows for rows in checks.find_anomalies(db, START, END).values())
