"""Offline US-07: published synthetic fixtures and actual retained demo rows."""
from datetime import date, timedelta
import json

from fastapi.testclient import TestClient
import pytest

from outage_explorer import analytics, auth
from outage_explorer.api import reconciliation
from outage_explorer.main import app
from test_datasets import settings, headers
from test_diagnostics import snapshot, START, END, editable


@pytest.fixture
def environment(snapshot, settings, monkeypatch):
    control = {'opened': 0, 'edit': None}
    def opening():
        control['opened'] += 1
        db = analytics.open_analytics(snapshot)
        if control['edit']: control['edit'](db)
        return db
    monkeypatch.setattr(reconciliation, 'open_analytics', opening)
    app.dependency_overrides[auth.get_auth_settings] = lambda: settings
    with TestClient(app) as client:
        yield client, control
    app.dependency_overrides.clear()


def codes(row):
    return {reason['code'] for reason in row['reasons']}


@pytest.mark.parametrize('role', ['analyst', 'admin'])
def test_defaults_exact_values_counts_and_read_only(environment, settings, snapshot, role):
    client, control = environment
    before = {p:p.read_bytes() for p in snapshot.rglob('*') if p.is_file()}
    response = client.get('/reconciliation', headers=headers(settings, role))
    assert response.status_code == 200
    result = response.json()
    assert result['start_date'] == str(START) and result['end_date'] == str(END)
    assert result['summary'] == dict(days_requested=4, days_compared=4, days_matching=4, days_with_differences=0, days_unavailable=0)
    assert result['source_completeness']['independently_verified'] is False
    row = result['rows'][0]
    assert row['national_capacity_mw'] == row['facility_capacity_mw'] == row['generator_capacity_mw'] == '300.000000'
    assert row['national_outage_mw'] == '30.000000'
    assert row['generator_capacity_minus_facility_mw'] == row['generator_minus_facility_mw'] == '0.000000'
    assert [row[level+'_count'] for level in ('national','facility','generator')] == [1,2,3]
    assert row['numeric_aggregates_match'] is True and codes(row) == {'aggregates_match'}
    assert response.headers['cache-control'] == 'no-store' and response.headers['vary'] == 'Authorization'
    assert control['opened'] == 1
    assert {p:p.read_bytes() for p in snapshot.rglob('*') if p.is_file()} == before
    assert str(snapshot) not in response.text and 'snapshot_version' not in response.text


@pytest.mark.parametrize('persona', [None, 'viewer', 'invalid'])
@pytest.mark.parametrize('path', ['/reconciliation', '/reconciliation/facilities?date=2026-01-01'])
def test_denial_precedes_any_data_access(environment, settings, persona, path):
    client, control = environment
    credential = {} if persona is None else {'Authorization': 'Bearer invalid'} if persona == 'invalid' else headers(settings, persona)
    response = client.get(path, headers=credential)
    assert response.status_code == (403 if persona == 'viewer' else 401)
    assert control['opened'] == 0


@pytest.mark.parametrize('query', ['start_date=bad', 'start_date=2026-02-02&end_date=2026-01-01',
    'start_date=2026-01-01&end_date=2026-02-01', 'start_date=2026-01-01&start_date=2026-01-02', 'dataset=facility_outages'])
def test_invalid_bounds_and_unknown_parameters(environment, settings, query):
    client, control = environment
    assert client.get('/reconciliation?'+query, headers=headers(settings)).status_code == 400
    assert control['opened'] == 0


def test_explicit_bounds_empty_ranges_and_absent_dates(environment, settings):
    client, control = environment
    def edit(db):
        for view in analytics.VIEWS:
            editable(db, view)
            db.execute(f'DELETE FROM {view} WHERE outage_date = ?', [START+timedelta(days=1)])
    control['edit'] = edit
    result = client.get('/reconciliation', headers=headers(settings)).json()
    assert len(result['rows']) == 4 and result['summary']['days_unavailable'] == 1
    missing = result['rows'][1]
    assert missing['status'] == 'missing_level' and missing['national_outage_mw'] is None
    assert {'missing_national_coverage','missing_facility_coverage','missing_generator_coverage'} <= codes(missing)
    empty = client.get('/reconciliation?start_date=2026-02-01&end_date=2026-02-03', headers=headers(settings)).json()
    assert empty['summary']['days_compared'] == 0 and empty['summary']['days_unavailable'] == 3
    assert all(row['national_count'] == row['facility_count'] == row['generator_count'] == 0 for row in empty['rows'])
    assert client.get('/reconciliation?end_date=0001-01-01', headers=headers(settings)).status_code == 200


def test_all_empty_views_and_missing_snapshot_have_clear_responses(environment, settings, monkeypatch):
    client, control = environment
    def empty(db):
        for view in analytics.VIEWS:
            editable(db, view); db.execute(f'DELETE FROM {view}')
    control['edit'] = empty
    response = client.get('/reconciliation', headers=headers(settings)).json()
    assert response['rows'] == [] and response['available_start'] is None
    def unavailable(): raise analytics.AnalyticsError('private path')
    monkeypatch.setattr(reconciliation, 'open_analytics', unavailable)
    error = client.get('/reconciliation', headers=headers(settings))
    assert error.status_code == 503 and 'private' not in error.text


@pytest.mark.parametrize('level,reason', [('facility','missing_facility_key'),('generator','missing_generator_group')])
def test_missing_keys_are_not_dropped_or_zero_filled(environment, settings, level, reason):
    client, control = environment
    def edit(db):
        editable(db, level+'_outages')
        db.execute(f"DELETE FROM {level}_outages WHERE facility_id='002' AND outage_date=?", [START])
    control['edit'] = edit
    daily = client.get('/reconciliation', headers=headers(settings)).json()['rows'][0]
    assert daily['status'] == 'difference' and reason in codes(daily)
    facilities = client.get('/reconciliation/facilities?date='+str(START), headers=headers(settings)).json()
    row = next(row for row in facilities['rows'] if row['facility_id']=='002')
    assert row['status'] == 'missing_level' and reason in codes(row)
    assert row['generator_minus_facility_mw'] is None
    assert row[level+'_capacity_mw'] is None


def test_zero_mw_orphan_is_a_key_difference_even_when_numeric_totals_match(environment, settings):
    client, control = environment
    def edit(db):
        for view in analytics.VIEWS: editable(db, view)
        db.execute('UPDATE national_outages SET capacity_mw=200, outage_mw=20 WHERE outage_date=?', [START])
        db.execute("DELETE FROM facility_outages WHERE facility_id='002' AND outage_date=?", [START])
        db.execute("UPDATE generator_outages SET capacity_mw=0, outage_mw=0 WHERE facility_id='002' AND outage_date=?", [START])
    control['edit'] = edit
    row = client.get('/reconciliation', headers=headers(settings)).json()['rows'][0]
    assert row['numeric_aggregates_match'] is True and row['status'] == 'difference'
    assert row['missing_facility_keys'] == 1 and 'missing_facility_key' in codes(row)


def test_exact_micro_mw_gaps_and_unknown_cause(environment, settings):
    client, control = environment
    def edit(db):
        editable(db,'facility_outages')
        db.execute("UPDATE facility_outages SET outage_mw=outage_mw+0.000001, capacity_mw=capacity_mw+0.000002 WHERE facility_id='001' AND outage_date=?", [START])
    control['edit'] = edit
    row = client.get('/reconciliation', headers=headers(settings)).json()['rows'][0]
    assert row['facility_minus_national_mw'] == '0.000001'
    assert row['generator_minus_facility_mw'] == '-0.000001'
    assert row['facility_capacity_minus_national_mw'] == '0.000002'
    assert {'outage_mismatch','capacity_mismatch','unknown_cause'} <= codes(row)


@pytest.mark.parametrize('fault,reason', [('null','invalid_values'),('duplicate','duplicate_keys'),('negative','unsupported_values')])
def test_ambiguous_or_unsupported_aggregates_are_withheld(environment, settings, fault, reason):
    client, control = environment
    def edit(db):
        editable(db, 'generator_outages')
        if fault == 'duplicate':
            db.execute('INSERT INTO generator_outages SELECT * FROM generator_outages WHERE outage_date=?', [START])
        else:
            db.execute('UPDATE generator_outages SET capacity_mw=' + ('NULL' if fault=='null' else '-1') + ' WHERE outage_date=?', [START])
    control['edit'] = edit
    row = client.get('/reconciliation', headers=headers(settings)).json()['rows'][0]
    assert row['status'] == 'invalid_data' and reason in codes(row)
    assert row['generator_capacity_mw'] is None and row['national_outage_mw'] is None
    result = client.get('/reconciliation/facilities?date='+str(START), headers=headers(settings)).json()
    assert all(row['status']=='invalid_data' and row['generator_capacity_mw'] is None for row in result['rows'])


def test_facility_pagination_and_limits(environment, settings):
    client, _ = environment
    base='/reconciliation/facilities?date='+str(START)
    first=client.get(base+'&page_size=1', headers=headers(settings)).json()
    second=client.get(base+'&page_size=1&page=2', headers=headers(settings)).json()
    assert first['total']==second['total']==2
    assert first['rows'][0]['facility_id']=='001' and second['rows'][0]['facility_id']=='002'
    assert client.get(base+'&page_size=101', headers=headers(settings)).status_code==400
    assert client.get(base+'&page=0', headers=headers(settings)).status_code==400
    assert client.get('/reconciliation/facilities', headers=headers(settings)).status_code==400
    assert client.get('/reconciliation/facilities?date=2025-01-01', headers=headers(settings)).json()['rows']==[]


def test_real_demo_all_thirty_days_and_fifty_five_facilities(tmp_path, settings):
    from outage_explorer.demo import create_demo_app
    from outage_explorer.demo_data import build_demo
    root=tmp_path/'eia-demo'; build_demo(root)
    before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    application=create_demo_app(root)
    application.dependency_overrides[auth.get_auth_settings]=lambda:settings
    with TestClient(application) as client:
        response=client.get('/reconciliation', headers=headers(settings))
        assert response.status_code==200
        result=response.json()
        assert result['start_date']=='2026-09-22' and result['end_date']=='2026-10-21'
        assert result['summary']['days_matching']==30 and result['summary']['days_with_differences']==0
        assert result['source_completeness']['mode']=='demo' and result['source_completeness']['independently_verified'] is False
        assert 'inconsistent totals' in result['source_completeness']['caveat']
        assert [sum(row[level+'_count'] for row in result['rows']) for level in ('national','facility','generator')]==[30,1650,2850]
        groups=client.get('/reconciliation/facilities?date=2026-10-21',headers=headers(settings)).json()
        assert groups['total']==55 and len(groups['rows'])==25
        assert all(row['status']=='equal' for row in groups['rows'])
        assert client.get('/reconciliation',headers=headers(settings,'viewer')).status_code==403
    assert {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}==before


def test_central_dataset_permissions_and_bad_viewer_params_still_precede_reads(environment, settings, monkeypatch):
    client, control = environment
    assert client.get('/reconciliation/facilities?date=bad&page_size=9999', headers=headers(settings, 'viewer')).status_code == 403
    monkeypatch.setitem(auth.DATASET_PERMISSIONS, 'generator_outages', frozenset())
    assert client.get('/reconciliation', headers=headers(settings)).status_code == 403
    assert control['opened'] == 0


def test_offsetting_facility_differences_cannot_hide_behind_matching_daily_totals(environment, settings):
    client, control = environment
    def edit(db):
        editable(db,'facility_outages')
        db.execute("UPDATE facility_outages SET outage_mw=outage_mw+CASE WHEN facility_id='001' THEN 1 ELSE -1 END WHERE outage_date=?", [START])
    control['edit'] = edit
    row = client.get('/reconciliation', headers=headers(settings)).json()['rows'][0]
    assert row['numeric_aggregates_match'] is True and row['status']=='difference'
    assert row['facility_minus_national_mw']=='0.000000' and row['facility_groups_with_differences']==2
    assert 'facility_group_mismatch' in codes(row)
    groups=client.get('/reconciliation/facilities?date='+str(START),headers=headers(settings)).json()['rows']
    assert all(group['status']=='difference' and 'outage_mismatch' in codes(group) for group in groups)
