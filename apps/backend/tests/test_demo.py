"""Real saved EIA data through actual FastAPI routes; no live EIA or browser."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import time

from fastapi.testclient import TestClient
import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from outage_explorer import analytics, auth, demo_data, main, refresh_eia
from outage_explorer.analysis_eia import digest
from outage_explorer.demo import create_demo_app
from outage_explorer.extract_eia import ExtractionError


@pytest.fixture(scope='module')
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp('real-demo') / 'eia-demo'
    demo_data.build_demo(root)
    return root


@pytest.fixture(scope='module')
def accounts():
    passwords = {role.value: secrets.token_urlsafe(24) for role in auth.Role}
    settings = auth.AuthSettings(secrets.token_bytes(32), {
        role.value: auth.Account(auth.User(username=role.value, role=role), auth.hash_password(passwords[role.value]))
        for role in auth.Role})
    return settings, passwords


@pytest.fixture
def environment(root, accounts, monkeypatch):
    def no_network(*_, **__):
        pytest.fail('Demo must never perform live network requests')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', no_network)
    application = create_demo_app(root)
    settings, passwords = accounts
    application.dependency_overrides[auth.get_auth_settings] = lambda: settings
    opened = []
    opener = application.state.analytics_opener
    def opening():
        opened.append(True)
        return opener()
    application.state.analytics_opener = opening
    with TestClient(application) as client:
        yield client, opened, settings, passwords
    application.dependency_overrides.clear()


def login(environment, role='analyst'):
    client, _, _, passwords = environment
    response = client.post('/auth/login', json={'username': role, 'password': passwords[role]})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    return {'Authorization': 'Bearer ' + response.json()['access_token']}


def test_materialized_demo_real_counts_types_and_production_isolation(root):
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    directory = demo_data.build_demo(root)
    assert before == {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    assert not list(root.rglob('current.json')) and not list(root.rglob('manifest.json'))
    assert not list(root.rglob('ingestion-runs'))
    metadata = json.loads((directory / 'demo.json').read_text())
    assert metadata['production_eligible'] is False
    assert metadata['routes']['facility-nuclear-outages']['received_rows'] == 1650
    assert metadata['routes']['facility-nuclear-outages']['reported_totals'] == [2850, 2850, 2850]
    assert metadata['routes']['facility-nuclear-outages']['count_agrees'] is False
    with demo_data.open_demo(root) as db:
        assert [db.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in analytics.VIEWS] == [30, 1650, 2850]
        description = db.execute('SELECT * FROM generator_outages LIMIT 0').description
        assert {column[0]: str(column[1]) for column in description}['capacity_mw'] == 'DECIMAL(24,6)'
    with pytest.raises(analytics.AnalyticsError, match='No published snapshot'):
        analytics.open_analytics(root)
    assert not hasattr(main.app.state, 'analytics_opener')  # Separate app, no global demo switch.


@pytest.mark.parametrize('role', ['viewer', 'analyst', 'admin'])
def test_login_catalog_and_current_user_real_data(environment, role):
    client, opened, _, _ = environment
    headers = login(environment, role)
    assert client.get('/auth/me', headers=headers).json() == {'username': role, 'role': role}
    response = client.get('/datasets', headers=headers)
    assert response.status_code == 200
    assert response.headers['x-outage-explorer-data-mode'] == 'demo-completeness-unverified'
    catalog = response.json()['datasets']
    assert [dataset['name'] for dataset in catalog] == (['national_outages'] if role == 'viewer' else list(analytics.VIEWS))
    assert all(dataset['description'].startswith(demo_data.NOTICE) for dataset in catalog)
    assert len(opened) == 1
    assert not any(str(root) in response.text for root in (demo_data.DEFAULT_DEMO_ROOT, demo_data.ROOT))


def test_unauthenticated_forbidden_and_unsafe_requests_never_open_demo(environment):
    client, opened, _, _ = environment
    assert client.get('/datasets').status_code == 401
    assert client.post('/auth/login', json={'username': 'viewer', 'password': 'wrong'}).status_code == 401
    headers = login(environment, 'viewer')
    for path in ('/datasets/facility_outages/preview', '/datasets/generator_outages/preview?facility_id=46'):
        assert client.get(path, headers=headers).status_code == 403
    assert client.post('/query', headers=headers, json={'sql': 'SELECT * FROM national_outages'}).status_code == 403
    assert client.post('/refresh', headers=headers, json={'end_date': '2026-10-21'}).status_code == 403
    assert client.get('/refresh/unknown', headers=headers).status_code == 403
    assert not opened
    headers = login(environment, 'analyst')
    for sql, status in [('SELECT * FROM secret_data', 403), ('SELECT * FROM read_parquet(\'/private/file\')', 400),
                        ('DELETE FROM national_outages', 400), ('SELECT * FROM national_outages; SELECT * FROM facility_outages', 400)]:
        assert client.post('/query', headers=headers, json={'sql': sql}).status_code == status
    assert client.post('/refresh', headers=headers, json={'end_date': '2026-10-21'}).status_code == 403
    assert not opened


def test_real_preview_pagination_filters_empty_and_errors(environment):
    client, _, _, _ = environment
    headers = login(environment)
    first = client.get('/datasets/generator_outages/preview?page_size=25', headers=headers).json()
    second = client.get('/datasets/generator_outages/preview?page_size=25&page=2', headers=headers).json()
    assert first['total'] == second['total'] == 2850
    key = lambda row: (row['outage_date'], row['facility_id'], row['generator_id'])
    assert len(first['rows']) == len(second['rows']) == 25
    assert set(map(key, first['rows'])).isdisjoint(set(map(key, second['rows'])))
    assert list(map(key, first['rows'])) == sorted(map(key, first['rows']))
    filtered = client.get('/datasets/facility_outages/preview?facility_id=46&start_date=2026-09-30&end_date=2026-10-01', headers=headers).json()
    assert filtered['total'] == 2
    assert [row['capacity_mw'] for row in filtered['rows']] == ['3661.700000', '3755.800000']
    empty = client.get('/datasets/facility_outages/preview?facility_id=not-a-facility', headers=headers).json()
    assert empty['total'] == 0 and empty['rows'] == []
    assert client.get('/datasets/national_outages/preview?facility_id=46', headers=headers).status_code == 400
    assert client.get('/datasets/national_outages/preview?page_size=201', headers=headers).status_code == 400
    assert client.get('/datasets/unknown/preview', headers=headers).status_code == 404


def test_real_sql_and_national_metric_preserve_values_and_scope(environment):
    client, _, _, _ = environment
    headers = login(environment)
    response = client.post('/query', headers=headers, json={
        'sql': "SELECT outage_date, facility_id, generator_id, capacity_mw FROM generator_outages WHERE facility_id = '46' ORDER BY outage_date, generator_id LIMIT 2"})
    assert response.status_code == 200
    assert response.json()['rows'] == [['2026-09-22', '46', '1', '1227.400000'], ['2026-09-22', '46', '2', '1207.700000']]
    capped = client.post('/query', headers=headers, json={'sql': 'SELECT * FROM generator_outages LIMIT 10000'}).json()
    assert capped['row_count'] == 200 and capped['truncated'] is True
    viewer = login(environment, 'viewer')
    metric = client.get('/metrics/fleet-offline-share?start_date=2026-10-20&end_date=2026-10-20&dataset=generator_outages&facility_id=46', headers=viewer)
    assert metric.status_code == 200
    assert metric.json()['rows'] == [{'outage_date': '2026-10-20', 'total_fleet_capacity_mw': '100056.700000',
        'offline_capacity_mw': '19859.767000', 'fleet_offline_share_pct': '19.848513', 'status': 'ok'}]
    assert 'facility' not in metric.text and 'generator' not in metric.text
    assert client.get('/metrics/generator-offline-share', headers=viewer).status_code == 404
    missing = client.get('/metrics/fleet-offline-share?start_date=2026-10-22&end_date=2026-10-22', headers=viewer).json()['rows'][0]
    assert missing['status'] == 'missing_date' and missing['fleet_offline_share_pct'] is None


def test_demo_admin_refresh_job_is_explicit_failure_without_mutation(environment, root):
    client, _, _, _ = environment
    headers = login(environment, 'admin')
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    response = client.post('/refresh', headers=headers, json={'end_date': '2026-10-21'})
    assert response.status_code == 202
    job_id = response.json()['job_id']
    assert response.json()['status'] == 'pending'
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = client.get('/refresh/' + job_id, headers=headers).json()
        if result['status'] == 'failed': break
        time.sleep(.005)
    assert result['status'] == 'failed' and result['published'] is False
    assert result['finished_at'] and result['started_at']
    assert result['error'].startswith('Read-only evidence demo:')
    assert result['row_counts'] == {} and result['fetched_rows'] == {}
    assert client.get('/refresh/no-such-job', headers=headers).status_code == 404
    assert client.get('/datasets', headers=headers).status_code == 200  # Readers remain usable.
    assert before == {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_invalid_expired_tokens_and_session_change_cannot_retain_access(environment, monkeypatch):
    client, opened, settings, _ = environment
    analyst = login(environment)
    assert client.get('/datasets/facility_outages/preview', headers=analyst).status_code == 200
    viewer = login(environment, 'viewer')
    assert client.get('/datasets/facility_outages/preview', headers=viewer).status_code == 403
    assert client.get('/datasets').status_code == 401  # Token is not a server cookie session.
    assert client.get('/datasets', headers={'Authorization': 'Bearer invalid'}).status_code == 401
    count = len(opened)
    issued = auth.time.time()
    monkeypatch.setattr(auth.time, 'time', lambda: issued + settings.token_seconds + 2)
    assert client.get('/datasets', headers=analyst).status_code == 401
    assert len(opened) == count


def test_missing_corrupt_demo_never_falls_back_to_production(root, tmp_path):
    missing = tmp_path / 'eia-demo'
    with pytest.raises(analytics.AnalyticsError, match='Demo data'):
        demo_data.open_demo(missing)
    shutil.copytree(root, missing)
    metadata = next(missing.rglob('demo.json'))
    payload = json.loads(metadata.read_text()); payload['production_eligible'] = True
    metadata.write_text(json.dumps(payload))
    with pytest.raises(analytics.AnalyticsError): demo_data.open_demo(missing)
    with pytest.raises(analytics.AnalyticsError): demo_data.build_demo(tmp_path / 'production')


def test_corrupt_physical_parquet_is_rejected(root, tmp_path):
    copied = tmp_path / 'eia-demo'; shutil.copytree(root, copied)
    path = next(copied.rglob('facility-nuclear-outages.parquet'))
    pq.write_table(pa.table({'wrong': [1]}), path)
    with pytest.raises(analytics.AnalyticsError): demo_data.open_demo(copied)


@pytest.mark.parametrize('fault', ['missing_facility', 'missing_generator', 'duplicate', 'inconsistent', 'truncated'])
def test_even_repinned_bad_demo_evidence_is_rejected(tmp_path, monkeypatch, fault):
    source = json.loads(gzip.decompress(demo_data.EVIDENCE.read_bytes()))
    route = 'generator-nuclear-outages' if fault == 'missing_generator' else 'facility-nuclear-outages'
    entry = source['routes'][route]
    if fault.startswith('missing_'): del entry['pages'][0]['response']['data'][0]
    elif fault == 'duplicate': entry['pages'][0]['response']['data'][1] = deepcopy(entry['pages'][0]['response']['data'][0])
    elif fault == 'inconsistent': entry['pages'][0]['response']['data'][0]['outage'] = str(Decimal(entry['pages'][0]['response']['data'][0]['outage']) + Decimal('.000001'))
    else: entry['pages'].pop()
    offset = 0
    for page in entry['pages']:
        page['query']['offset'] = offset
        offset += len(page['response']['data'])
        page['sha256'] = digest(page['response'])
    evidence = tmp_path / 'source.json.gz'; evidence.write_bytes(gzip.compress(json.dumps(source).encode()))
    summary = tmp_path / 'summary.json'; summary.write_text(json.dumps({'source_file_sha256': hashlib.sha256(evidence.read_bytes()).hexdigest()}))
    monkeypatch.setattr(demo_data, 'EVIDENCE', evidence); monkeypatch.setattr(demo_data, 'SUMMARY', summary)
    root = tmp_path / 'eia-demo'
    with pytest.raises((analytics.AnalyticsError, ExtractionError)): demo_data.build_demo(root)
    assert not root.exists()


def test_demo_cannot_be_adopted_as_a_production_base(root, tmp_path):
    production = tmp_path / 'production'
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('No HTTP permitted'))) as client:
        with pytest.raises(ExtractionError):
            refresh_eia.refresh(client, 'fake-key', date(2026, 10, 21), production, base_snapshot=root, emit=lambda _: None)
    assert not (production / 'current.json').exists()
    assert not list(root.rglob('current.json'))


def test_demo_count_caveat_cannot_silently_be_rewritten(root, tmp_path):
    copied = tmp_path / 'eia-demo'; shutil.copytree(root, copied)
    metadata = next(copied.rglob('demo.json'))
    payload = json.loads(metadata.read_text())
    payload['routes']['facility-nuclear-outages']['count_agrees'] = True
    payload['routes']['facility-nuclear-outages']['reported_totals'] = [1650, 1650, 1650]
    metadata.write_text(json.dumps(payload))
    with pytest.raises(analytics.AnalyticsError): demo_data.open_demo(copied)
