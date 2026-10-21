"""Offline API authorization and previews over small synthetic Parquet snapshots."""

from datetime import date, timedelta
from decimal import Decimal
import json
import secrets

from fastapi.testclient import TestClient
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from outage_explorer import analytics, auth
from outage_explorer.api import datasets
from outage_explorer.extract_eia import IDENTIFIERS, MEASURES, schema_for
from outage_explorer.main import app

START = date(2026, 1, 1)


@pytest.fixture(scope='module')
def settings():
    return auth.AuthSettings(secrets.token_bytes(32), {
        role.value: auth.Account(auth.User(username=role.value, role=role),
                                 auth.hash_password(secrets.token_urlsafe(24))) for role in auth.Role})


@pytest.fixture
def snapshot(tmp_path):
    directory = tmp_path / 'runs' / ('a' * 32)
    directory.mkdir(parents=True)
    manifest = {'schema_version': 1, 'start': str(START), 'end': str(START + timedelta(days=2)), 'routes': {}}
    for route, ids in IDENTIFIERS.items():
        identities = [{}]
        if ids:
            identities = [{'facility': '002', 'facilityName': 'Other'}, {'facility': '001', 'facilityName': None}]
        if 'generator' in ids:
            identities = [{**identities[0], 'generator': '01'}, {**identities[1], 'generator': '02'},
                          {**identities[1], 'generator': '01A'}]
        rows = []
        # Deliberately write in reverse date/entity order: SQL must sort explicitly.
        for offset in (2, 1, 0):
            for identity in identities:
                row = {'period': START + timedelta(days=offset), **identity,
                       'capacity': Decimal('123456789012345678.123456'),
                       'outage': Decimal('1.000001'), 'percentOutage': Decimal('2.123456')}
                row.update({f'{measure}-units': units for measure, units in MEASURES.items()})
                rows.append(row)
        pq.write_table(pa.Table.from_pylist(rows, schema=schema_for(route)), directory / f'{route}.parquet')
        manifest['routes'][route] = {'file': f'{route}.parquet', 'records': len(rows), 'candidate_key': ['period', *ids]}
    (directory / 'manifest.json').write_text(json.dumps(manifest))
    (tmp_path / 'current.json').write_text(json.dumps({'run': 'runs/' + 'a' * 32}))
    return tmp_path


@pytest.fixture
def client(settings, snapshot, monkeypatch):
    opened = []
    def opening():
        opened.append(True)
        return analytics.open_analytics(snapshot)
    monkeypatch.setattr(datasets, 'open_analytics', opening)
    app.dependency_overrides[auth.get_auth_settings] = lambda: settings
    with TestClient(app) as client:
        client.opened = opened
        yield client
    app.dependency_overrides.clear()


def headers(settings, role='analyst'):
    return {'Authorization': 'Bearer ' + auth.issue_token(settings.accounts[role].user, settings)}


@pytest.mark.parametrize('role', list(auth.Role))
def test_catalog_visibility_types_and_no_private_metadata(client, settings, role, snapshot):
    response = client.get('/datasets', headers=headers(settings, role))
    assert response.status_code == 200
    names = [item['name'] for item in response.json()['datasets']]
    assert names == (['national_outages'] if role == auth.Role.VIEWER else list(datasets.DATASETS))
    for item in response.json()['datasets']:
        assert set(item) == {'name', 'description', 'columns'} and item['description']
        columns = {c['name']: c['type'] for c in item['columns']}
        assert columns['outage_date'] == 'DATE'
        assert columns['capacity_mw'] == columns['outage_mw'] == 'DECIMAL(24,6)'
    assert str(snapshot) not in response.text and 'manifest' not in response.text and 'runs/' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['vary'] == 'Authorization'


@pytest.mark.parametrize('path', ['/datasets', '/datasets/national_outages/preview',
                                  '/datasets/facility_outages/preview', '/datasets/generator_outages/preview'])
@pytest.mark.parametrize('authorization', [None, 'Bearer invalid'])
def test_unauthenticated_requests_never_open_data(client, path, authorization):
    response = client.get(path, headers={'Authorization': authorization} if authorization else {})
    assert response.status_code == 401 and client.opened == []


@pytest.mark.parametrize('name', ['facility_outages', 'generator_outages'])
@pytest.mark.parametrize('query', ['', '?facility_id=001', '?page_size=bad&start_date=bad', '?dataset_name=national_outages'])
def test_viewer_direct_url_and_parameter_bypasses_are_denied_before_data(client, settings, name, query):
    response = client.get(f'/datasets/{name}/preview' + query, headers=headers(settings, 'viewer'))
    assert response.status_code == 403 and client.opened == []
    assert response.json() == {'detail': 'Forbidden.'}


@pytest.mark.parametrize('role', list(auth.Role))
def test_unknown_or_injected_dataset_is_404_before_data(client, settings, role):
    for name in ['unknown', 'national_outages;SELECT 1', 'National_Outages', 'us-nuclear-outages']:
        response = client.get(f'/datasets/{name}/preview', headers=headers(settings, role))
        assert response.status_code == 404
    assert client.opened == []


def test_viewer_national_preview_date_decimal_and_null_encoding(client, settings):
    response = client.get('/datasets/national_outages/preview', headers=headers(settings, 'viewer'))
    assert response.status_code == 200
    data = response.json()
    assert data['page'] == 1 and data['page_size'] == 50 and data['total'] == 3
    assert [row['outage_date'] for row in data['rows']] == ['2026-01-01', '2026-01-02', '2026-01-03']
    assert data['rows'][0]['capacity_mw'] == '123456789012345678.123456'
    assert data['rows'][0]['outage_mw'] == '1.000001'
    assert all('facility_id' not in row and 'generator_id' not in row for row in data['rows'])
    response = client.get('/datasets/facility_outages/preview?facility_id=001', headers=headers(settings))
    assert response.json()['rows'][0]['facility_name'] is None


@pytest.mark.parametrize('name,expected', [('facility_outages', 2), ('generator_outages', 4)])
def test_inclusive_date_and_exact_facility_filters(client, settings, name, expected):
    response = client.get(f'/datasets/{name}/preview?start_date=2026-01-02&end_date=2026-01-03&facility_id=001',
                          headers=headers(settings))
    assert response.status_code == 200 and response.json()['total'] == expected
    assert all(r['facility_id'] == '001' and r['outage_date'] >= '2026-01-02' for r in response.json()['rows'])
    for query in ['start_date=2026-01-03', 'end_date=2026-01-01']:
        response = client.get('/datasets/national_outages/preview?' + query, headers=headers(settings))
        assert response.json()['total'] == 1


def test_pagination_orders_all_key_columns_and_keeps_filtered_total(client, settings):
    rows = []
    for page in (1, 2, 3, 4, 5):
        response = client.get(f'/datasets/generator_outages/preview?page={page}&page_size=2', headers=headers(settings))
        assert response.status_code == 200 and response.json()['total'] == 9
        rows.extend(response.json()['rows'])
    keys = [(r['outage_date'], r['facility_id'], r['generator_id']) for r in rows]
    assert keys == sorted(keys) and len(set(keys)) == 9
    first = client.get('/datasets/generator_outages/preview?page_size=2', headers=headers(settings)).json()
    assert first['rows'] == rows[:2]
    assert client.get('/datasets/generator_outages/preview?page_size=200', headers=headers(settings)).status_code == 200


@pytest.mark.parametrize('query', ['facility_id=missing', "facility_id=001'%20OR%201=1--", 'start_date=2027-01-01', 'page=999'])
def test_empty_results_and_parameterized_injection(client, settings, query):
    response = client.get('/datasets/facility_outages/preview?' + query, headers=headers(settings))
    assert response.status_code == 200 and response.json()['rows'] == []
    assert response.json()['total'] == (6 if query == 'page=999' else 0)


@pytest.mark.parametrize('query', ['page=0', 'page=-1', 'page=abc', 'page_size=0', 'page_size=201',
                                   'page_size=1.5', 'page_size=9999999999999999999999',
                                   'page=2147483648', 'start_date=bad', 'start_date=2026-02-30',
                                   'start_date=2026-01-03&end_date=2026-01-01',
                                   'facility_id=', 'facility_id=%20', 'facility_id=%20001',
                                   'unexpected=1', 'page_size=1&page_size=200'])
def test_invalid_filters_return_400_before_snapshot_access(client, settings, query):
    response = client.get('/datasets/facility_outages/preview?' + query, headers=headers(settings))
    assert response.status_code == 400 and client.opened == []
    assert 'input' not in response.text


def test_national_facility_filter_is_rejected_before_data(client, settings):
    response = client.get('/datasets/national_outages/preview?facility_id=001', headers=headers(settings, 'viewer'))
    assert response.status_code == 400 and client.opened == []


@pytest.mark.parametrize('role', ['analyst', 'admin'])
@pytest.mark.parametrize('name', list(datasets.DATASETS))
def test_privileged_personas_can_preview_their_datasets(client, settings, role, name):
    assert client.get(f'/datasets/{name}/preview', headers=headers(settings, role)).status_code == 200


@pytest.mark.parametrize('path', ['/datasets', '/datasets/national_outages/preview'])
def test_missing_or_incompatible_snapshot_is_safe_503(client, settings, snapshot, path):
    (snapshot / 'current.json').unlink()
    response = client.get(path, headers=headers(settings))
    assert response.status_code == 503 and response.json() == {'detail': 'Published data is unavailable.'}
    assert str(snapshot) not in response.text
    (snapshot / 'current.json').write_text('{malformed')
    response = client.get(path, headers=headers(settings))
    assert response.status_code == 503 and response.json() == {'detail': 'Published data is unavailable.'}


def test_count_and_page_share_one_pinned_connection(client, settings, snapshot, monkeypatch):
    actual = datasets.open_analytics
    def open_then_switch_pointer():
        db = actual()
        (snapshot / 'current.json').write_text('{unusable-next-publication')
        return db
    monkeypatch.setattr(datasets, 'open_analytics', open_then_switch_pointer)
    response = client.get('/datasets/generator_outages/preview?page_size=2', headers=headers(settings))
    assert response.status_code == 200
    assert response.json()['total'] == 9 and len(response.json()['rows']) == 2
    assert len(client.opened) == 1


@pytest.mark.parametrize('path', ['/datasets', '/datasets/national_outages/preview'])
def test_expired_tokens_cannot_open_data(client, settings, monkeypatch, path):
    issued = 2_000_000_000
    monkeypatch.setattr(auth.time, 'time', lambda: issued)
    token = headers(settings)
    monkeypatch.setattr(auth.time, 'time', lambda: issued + settings.token_seconds)
    response = client.get(path, headers=token)
    assert response.status_code == 401 and client.opened == []
