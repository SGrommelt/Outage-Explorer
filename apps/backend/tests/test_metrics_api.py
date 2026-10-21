"""Offline US-08 API tests reuse synthetic published fixtures and real calculation."""
from contextlib import contextmanager
from datetime import date, timedelta
import secrets

from fastapi.testclient import TestClient
import pytest

from outage_explorer import analytics, auth
from outage_explorer.api import metrics
from outage_explorer.main import app
from test_capacity import publish, START, END


@pytest.fixture(scope='module')
def settings():
    return auth.AuthSettings(secrets.token_bytes(32), {
        role.value: auth.Account(auth.User(username=role.value, role=role), '') for role in auth.Role})


@pytest.fixture
def client(tmp_path, monkeypatch, settings):
    calls = []
    def opening():
        calls.append(True)
        return analytics.open_analytics(tmp_path)
    monkeypatch.setattr(metrics, 'open_analytics', opening)
    app.dependency_overrides[auth.get_auth_settings] = lambda: settings
    with TestClient(app) as client:
        client.opened = calls
        yield client
    app.dependency_overrides.clear()


def headers(settings, role='analyst'):
    return {'Authorization': 'Bearer ' + auth.issue_token(settings.accounts[role].user, settings)}


def url(start=START, end=END):
    return f'/metrics/fleet-offline-share?start_date={start}&end_date={end}'


@pytest.mark.parametrize('role', list(auth.Role))
def test_all_national_authorized_roles_get_exact_metric(client, settings, tmp_path, role):
    publish(tmp_path, capacity='300', outage='100')
    response = client.get(url(), headers=headers(settings, role))
    assert response.status_code == 200
    assert response.json() == {'rows': [
        {'outage_date': str(day), 'total_fleet_capacity_mw': '300.000000',
         'offline_capacity_mw': '100.000000', 'fleet_offline_share_pct': '33.333333', 'status': 'ok'}
        for day in (START, END)]}
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['vary'] == 'Authorization'
    assert str(tmp_path) not in response.text and 'facility' not in response.text and 'generator' not in response.text
    assert len(client.opened) == 1


@pytest.mark.parametrize('credential', [None, 'Bearer invalid'])
def test_authentication_before_snapshot(client, credential):
    response = client.get(url(), headers={'Authorization': credential} if credential else {})
    assert response.status_code == 401 and not client.opened


@pytest.mark.parametrize('start,end', [(END, START), (START, START + timedelta(days=366)), ('bad', END)])
def test_invalid_windows_before_snapshot(client, settings, start, end):
    response = client.get(url(start, end), headers=headers(settings))
    assert response.status_code == 400 and not client.opened


def test_missing_snapshot_safe_error(client, settings, tmp_path):
    response = client.get(url(), headers=headers(settings))
    assert response.status_code == 503 and response.json() == {'detail': 'Published data is unavailable.'}
    assert str(tmp_path) not in response.text


@pytest.mark.parametrize('capacity,outage,status', [('0', '0', 'zero_capacity'), ('100', '101', 'inconsistent_values'), ('-1', '0', 'unsupported_values')])
def test_invalid_percentages_are_not_zero(client, settings, tmp_path, capacity, outage, status):
    publish(tmp_path, capacity=capacity, outage=outage)
    response = client.get(url(), headers=headers(settings))
    assert response.status_code == 200
    assert all(row['status'] == status and row['fleet_offline_share_pct'] is None for row in response.json()['rows'])


def test_missing_dates_explicit(client, settings, tmp_path):
    publish(tmp_path)
    response = client.get(url(START, END + timedelta(days=1)), headers=headers(settings))
    assert response.status_code == 200
    assert response.json()['rows'][-1] == {
        'outage_date': str(END + timedelta(days=1)), 'total_fleet_capacity_mw': None,
        'offline_capacity_mw': None, 'fleet_offline_share_pct': None, 'status': 'missing_date'}


@pytest.mark.parametrize('issue', ['duplicate', 'missing'])
def test_defensive_calculation_status_preserved(client, settings, tmp_path, monkeypatch, issue):
    publish(tmp_path)
    @contextmanager
    def opening():
        with analytics.open_analytics(tmp_path) as db:
            db.execute('CREATE OR REPLACE TEMP TABLE national_outages AS SELECT * FROM national_outages')
            if issue == 'duplicate':
                db.execute('INSERT INTO national_outages SELECT * FROM national_outages')
            else:
                db.execute('UPDATE national_outages SET capacity_mw = NULL')
            yield db
    # Production publication validation rejects duplicates/null physical values.
    # These deliberately altered views exercise the existing defensive calculation.
    monkeypatch.setattr(metrics, 'open_analytics', opening)
    response = client.get(url(), headers=headers(settings))
    assert response.status_code == 200
    assert len(response.json()['rows']) == 2
    assert all(row['status'] == ('duplicate_key' if issue == 'duplicate' else 'missing_values')
               and row['fleet_offline_share_pct'] is None for row in response.json()['rows'])


def test_incompatible_physical_schema_is_unavailable(client, settings, tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    directory = publish(tmp_path)
    pq.write_table(pa.table({'unexpected': [1]}), directory / 'us-nuclear-outages.parquet')
    response = client.get(url(), headers=headers(settings))
    assert response.status_code == 503 and str(tmp_path) not in response.text


def test_missing_required_dates(client, settings):
    response = client.get('/metrics/fleet-offline-share', headers=headers(settings))
    assert response.status_code == 400 and not client.opened


def test_maximum_window_is_inclusive(client, settings, tmp_path):
    publish(tmp_path)
    response = client.get(url(START, START + timedelta(days=365)), headers=headers(settings))
    assert response.status_code == 200 and len(response.json()['rows']) == 366


def test_central_dataset_policy_checked_before_snapshot(client, settings, monkeypatch):
    monkeypatch.setitem(auth.DATASET_PERMISSIONS, 'national_outages', frozenset())
    response = client.get(url(), headers=headers(settings))
    assert response.status_code == 403 and not client.opened
