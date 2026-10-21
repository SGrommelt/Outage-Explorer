"""Offline SQL subset, resource escape, authorization, and cap regressions."""

from datetime import date, timedelta
from decimal import Decimal
import json

from fastapi.testclient import TestClient
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from outage_explorer import analytics, auth
from outage_explorer.api import query
from outage_explorer.extract_eia import IDENTIFIERS, MEASURES, schema_for
from outage_explorer.main import app
from test_datasets import settings  # Reuse the configured offline personas.

START = date(2026, 1, 1)


@pytest.fixture(scope='module')
def query_snapshot(tmp_path_factory):
    root = tmp_path_factory.mktemp('sql-snapshot')
    directory = root / 'runs' / ('a' * 32)
    directory.mkdir(parents=True)
    manifest = {'schema_version': 1, 'start': str(START), 'end': str(START + timedelta(days=204)), 'routes': {}}
    for route, ids in IDENTIFIERS.items():
        rows = []
        for offset in range(205):
            row = {'period': START + timedelta(days=offset), 'capacity': Decimal('123456789012345678.123456'),
                   'outage': Decimal('1.000001'), 'percentOutage': Decimal('2.123456')}
            if ids:
                row.update(facility='001', facilityName=None)
            if 'generator' in ids:
                row['generator'] = '01A'
            row.update({f'{measure}-units': units for measure, units in MEASURES.items()})
            rows.append(row)
        pq.write_table(pa.Table.from_pylist(rows, schema=schema_for(route)), directory / f'{route}.parquet')
        manifest['routes'][route] = {'file': f'{route}.parquet', 'records': len(rows), 'candidate_key': ['period', *ids]}
    (directory / 'manifest.json').write_text(json.dumps(manifest))
    (root / 'current.json').write_text(json.dumps({'run': 'runs/' + 'a' * 32}))
    return root


@pytest.fixture
def client(query_snapshot, settings, monkeypatch):
    opened = []
    def opening():
        opened.append(True)
        return analytics.open_analytics(query_snapshot)
    monkeypatch.setattr(query, 'open_analytics', opening)
    app.dependency_overrides[auth.get_auth_settings] = lambda: settings
    with TestClient(app) as client:
        client.opened = opened
        yield client
    app.dependency_overrides.clear()


def headers(settings, role='analyst'):
    return {'Authorization': 'Bearer ' + auth.issue_token(settings.accounts[role].user, settings)}


def post(client, settings, sql):
    return client.post('/query', json={'sql': sql}, headers=headers(settings))


@pytest.mark.parametrize('role', ['analyst', 'admin'])
@pytest.mark.parametrize('dataset', list(query.DATASETS))
def test_authorized_select_and_consistent_serialization(client, settings, role, dataset):
    response = client.post('/query', json={'sql': f'SELECT * FROM {dataset} LIMIT 2'}, headers=headers(settings, role))
    assert response.status_code == 200
    data = response.json()
    assert data['row_count'] == 2 and data['row_limit'] == 200 and not data['truncated']
    columns = [c['name'] for c in data['columns']]
    first = dict(zip(columns, data['rows'][0]))
    assert first['outage_date'] == '2026-01-01'
    assert first['capacity_mw'] == '123456789012345678.123456'
    assert first['outage_mw'] == '1.000001'
    assert data['columns'][columns.index('capacity_mw')]['type'] == 'DECIMAL(24,6)'
    if dataset != 'national_outages':
        assert first['facility_id'] == '001' and first['facility_name'] is None
    assert response.headers['cache-control'] == 'no-store' and response.headers['vary'] == 'Authorization'
    assert 'runs/' not in response.text and '/private/' not in response.text


@pytest.mark.parametrize('sql', ['SELECT * FROM national_outages', 'SELECT * FROM generator_outages', 'not SQL'])
def test_viewer_denied_before_sql_validation_or_data(client, settings, monkeypatch, sql):
    def must_not_validate(*args):
        pytest.fail('Viewer must be denied before SQL validation')
    monkeypatch.setattr(query, 'prepare_query', must_not_validate)
    response = client.post('/query', json={'sql': sql}, headers=headers(settings, 'viewer'))
    assert response.status_code == 403 and client.opened == []


@pytest.mark.parametrize('token', [None, 'Bearer invalid'])
def test_authentication_required_before_data(client, token):
    response = client.post('/query', json={'sql': 'SELECT * FROM national_outages'},
                           headers={'Authorization': token} if token else {})
    assert response.status_code == 401 and client.opened == []


@pytest.mark.parametrize('sql', [
    'SELECT * FROM unknown', 'SELECT * FROM sqlite_master', 'SELECT * FROM duckdb_settings',
    'SELECT * FROM information_schema_tables', "SELECT * FROM '/etc/passwd'",
])
def test_unknown_datasets_are_denied_before_opening(client, settings, sql):
    response = post(client, settings, sql)
    assert response.status_code == 403 and response.json() == {'detail': 'Forbidden.'}
    assert client.opened == []


def test_per_table_policy_is_applied_before_opening(client, settings, monkeypatch):
    monkeypatch.setitem(auth.DATASET_PERMISSIONS, 'generator_outages', frozenset((auth.Role.ADMIN,)))
    assert post(client, settings, 'SELECT * FROM generator_outages').status_code == 403
    assert client.opened == []


REJECTED = [
    'SELECT * FROM national_outages; SELECT * FROM generator_outages',
    'DELETE FROM national_outages', 'UPDATE national_outages SET outage_mw=0',
    'INSERT INTO national_outages VALUES (1)', 'CREATE TABLE stolen AS SELECT * FROM national_outages',
    'DROP VIEW national_outages', 'ALTER TABLE national_outages RENAME TO stolen',
    "COPY national_outages TO '/tmp/stolen.csv'", "ATTACH '/tmp/stolen.duckdb'", 'DETACH memory',
    'PRAGMA database_list', 'SET enable_external_access=true', 'INSTALL httpfs', 'LOAD httpfs',
    'SELECT * INTO stolen FROM national_outages',
    "SELECT * FROM read_parquet('/tmp/private.parquet')", "SELECT * FROM read_csv_auto('/etc/passwd')",
    "SELECT * FROM glob('/tmp/*')", 'SELECT * FROM duckdb_tables()',
    "SELECT read_blob('/etc/passwd') FROM national_outages", "SELECT read_text('/etc/passwd') FROM national_outages",
    "SELECT getenv('HOME') FROM national_outages", "SELECT getvariable('secret') FROM national_outages",
    "SELECT nextval('sequence') FROM national_outages", "SELECT setseed(0.1) FROM national_outages",
    "SELECT query('SELECT * FROM generator_outages') FROM national_outages",
    "SELECT * FROM query_table('generator_outages')", "SELECT * FROM parquet_scan('https://example.invalid/file')",
    'SELECT * FROM main.national_outages', 'SELECT * FROM information_schema.tables',
    'SELECT * FROM national_outages n', 'SELECT n.* FROM national_outages n',
    'SELECT * FROM national_outages JOIN facility_outages USING (outage_date)',
    'SELECT * FROM national_outages, generator_outages',
    'SELECT * FROM (SELECT * FROM generator_outages)',
    'SELECT (SELECT outage_mw FROM generator_outages LIMIT 1) FROM national_outages',
    'SELECT * FROM national_outages WHERE EXISTS (SELECT * FROM unknown)',
    'SELECT * FROM national_outages WHERE outage_mw IN (SELECT outage_mw FROM generator_outages)',
    'WITH x AS (SELECT * FROM generator_outages) SELECT * FROM x',
    'SELECT * FROM national_outages UNION ALL SELECT * FROM generator_outages',
    'SELECT COUNT(*) FROM national_outages', 'SELECT outage_mw + 1 FROM national_outages',
    'SELECT outage_mw AS modified FROM national_outages', 'SELECT DISTINCT outage_date FROM national_outages',
    'SELECT * EXCLUDE (outage_mw) FROM national_outages', 'SELECT COLUMNS(*) FROM national_outages',
    "SELECT * REPLACE (read_blob('/etc/passwd') AS outage_mw) FROM national_outages",
    'SELECT * FROM national_outages USING SAMPLE 10%', 'SELECT * FROM national_outages ORDER BY random()',
    'SELECT * FROM national_outages LIMIT ALL', 'SELECT * FROM national_outages LIMIT 1 OFFSET 2',
    'SELECT * FROM national_outages LIMIT -1', 'SELECT * FROM national_outages LIMIT 1.5',
    'SELECT * FROM national_outages LIMIT 99999999999999999999999999999',
    "SELECT CAST(read_text('/etc/passwd') AS VARCHAR) FROM national_outages",
    "SELECT * FROM national_outages WHERE outage_mw = CAST(read_text('/etc/passwd') AS DECIMAL)",
    'SELECT * FROM national_outages WHERE outage_mw > 1e9999999',
    'SELECT missing_column FROM national_outages', 'SELECT 1', 'SELECT', '', '  ',
    'SELECT * FROM national_outages WHERE (',
]


@pytest.mark.parametrize('sql', REJECTED)
def test_unsupported_sql_and_escape_paths_never_reach_duckdb(client, settings, sql):
    response = post(client, settings, sql)
    assert response.status_code == 400 and client.opened == []
    assert response.json() in ({'detail': 'Invalid or unsupported query.'}, {'detail': 'Invalid request.'})
    assert '/etc/' not in response.text and '/tmp/' not in response.text


def test_valid_where_order_literals_dates_nulls_and_in(client, settings):
    sql = """SELECT outage_date, outage_mw, facility_name FROM facility_outages
             WHERE outage_date BETWEEN DATE '2026-01-02' AND DATE '2026-01-05'
               AND facility_id IN ('001', '002') AND facility_name IS NULL
               AND NOT (outage_mw < -1 OR outage_mw > 2)
             ORDER BY outage_date DESC LIMIT 3"""
    response = post(client, settings, sql)
    assert response.status_code == 200
    assert [row[0] for row in response.json()['rows']] == ['2026-01-05', '2026-01-04', '2026-01-03']
    assert all(row[2] is None for row in response.json()['rows'])


def test_client_literals_are_parameters_not_executable_sql(client, settings):
    literal = "COPY private TO '/tmp/secret'; SELECT read_blob('/etc/passwd')"
    sql = "SELECT * FROM facility_outages WHERE facility_name = '" + literal.replace("'", "''") + "'"
    prepared, parameters = query.prepare_query(sql, settings.accounts['analyst'].user)
    assert literal in parameters and '/tmp/secret' not in prepared and 'read_blob' not in prepared
    response = post(client, settings, sql)
    assert response.status_code == 200 and response.json()['rows'] == []


@pytest.mark.parametrize('limit,count,truncated', [(None, 200, True), (0, 0, False), (1, 1, False),
                                                  (199, 199, False), (200, 200, False),
                                                  (201, 200, True), (1000000, 200, True)])
def test_result_cap_cannot_be_bypassed(client, settings, limit, count, truncated):
    sql = 'SELECT outage_date FROM national_outages ORDER BY outage_date'
    if limit is not None:
        sql += f' LIMIT {limit}'
    response = post(client, settings, sql)
    assert response.status_code == 200
    data = response.json()
    assert data['row_count'] == len(data['rows']) == count and data['truncated'] == truncated
    assert data['row_limit'] == 200


def test_empty_results_keep_column_types(client, settings):
    response = post(client, settings, "SELECT outage_date, outage_mw FROM national_outages WHERE outage_mw > 100")
    assert response.status_code == 200
    assert response.json()['rows'] == [] and not response.json()['truncated']
    assert response.json()['columns'] == [{'name': 'outage_date', 'type': 'DATE'}, {'name': 'outage_mw', 'type': 'DECIMAL(24,6)'}]


def test_duplicate_projection_names_do_not_discard_values(client, settings):
    response = post(client, settings, 'SELECT outage_mw, outage_mw FROM national_outages LIMIT 1')
    assert response.status_code == 200 and response.json()['rows'] == [['1.000001', '1.000001']]
    assert [c['name'] for c in response.json()['columns']] == ['outage_mw', 'outage_mw']


@pytest.mark.parametrize('payload', [{}, {'sql': 'x' * 10001}, {'sql': 'SELECT * FROM national_outages', 'limit': 1000000}, {'sql': []}])
def test_bad_request_schema_is_generic_and_does_not_open_data(client, settings, payload):
    response = client.post('/query', json=payload, headers=headers(settings))
    assert response.status_code == 400 and response.json() == {'detail': 'Invalid request.'}
    assert client.opened == []


def test_runtime_and_storage_errors_do_not_leak_details(client, settings, monkeypatch):
    response = post(client, settings, "SELECT * FROM national_outages WHERE outage_date > 'private-invalid-date'")
    assert response.status_code == 400 and 'private-invalid-date' not in response.text
    def unavailable():
        raise analytics.AnalyticsError('/private/snapshot/secrets')
    monkeypatch.setattr(query, 'open_analytics', unavailable)
    response = post(client, settings, 'SELECT * FROM national_outages')
    assert response.status_code == 503 and response.json() == {'detail': 'Published data is unavailable.'}


def test_query_is_pinned_read_only_and_does_not_replace_snapshot(client, settings, query_snapshot, monkeypatch):
    before = {p: p.read_bytes() for p in query_snapshot.rglob('*') if p.is_file()}
    actual = query.open_analytics
    def switch_pointer_after_open():
        db = actual()
        (query_snapshot / 'current.json').write_text('{not-a-published-snapshot')
        return db
    monkeypatch.setattr(query, 'open_analytics', switch_pointer_after_open)
    try:
        response = post(client, settings, 'SELECT * FROM national_outages LIMIT 1')
        assert response.status_code == 200
        assert all(p.read_bytes() == value for p, value in before.items() if p.name != 'current.json')
        assert len(client.opened) == 1
    finally:
        (query_snapshot / 'current.json').write_bytes(before[query_snapshot / 'current.json'])


@pytest.mark.parametrize('sql', [
    'SELECT * FROM national_outages FOR UPDATE',
    'SELECT * FROM national_outages;;',
    'EXPLAIN SELECT * FROM national_outages',
    "SELECT * FROM national_outages WHERE length(read_blob('/etc/passwd')) > 0",
    "SELECT * FROM national_outages ORDER BY read_text('/etc/passwd')",
    'SELECT * FROM national_outages WHERE ' + ' OR '.join(['outage_mw = 1'] * 200),
])
def test_remaining_clauses_and_ast_budget_are_rejected_before_access(client, settings, sql):
    assert post(client, settings, sql).status_code == 400
    assert client.opened == []


def test_comments_quoted_columns_and_single_trailing_semicolon_are_safe(client, settings):
    response = post(client, settings, '/* COPY; DROP; */ SELECT "outage_date" FROM "national_outages" LIMIT 1; -- harmless')
    assert response.status_code == 200 and response.json()['rows'] == [['2026-01-01']]
