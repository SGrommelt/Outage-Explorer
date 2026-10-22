"""Offline background refresh API; workers use controlled/mocked ingestion only."""

from concurrent.futures import ThreadPoolExecutor
import json
import secrets
import shutil
from threading import Barrier, Event
import time

from fastapi.testclient import TestClient
import pytest

from outage_explorer import analytics, auth, main, refresh_jobs
from outage_explorer.api import datasets, query
from test_datasets import settings, snapshot
from test_analytics import publish

RUN = 'b' * 32
BODY = {'end_date': '2026-01-03'}


def success():
    return {'schema_version': 2, 'ingestion_run': {'status': 'published'},
            'routes': {route: {'records': n} for route, n in zip(refresh_jobs.ROUTE_NAMES, (3, 6, 9))}}


def headers(settings, role='admin'):
    return {'Authorization': 'Bearer ' + auth.issue_token(settings.accounts[role].user, settings)}


@pytest.fixture
def environment(settings, snapshot, monkeypatch):
    control = {'calls': [], 'admitted': [], 'lookups': [], 'release': Event(), 'entered': Event(),
               'runner': lambda request, root, emit: success()}
    def runner(request, root, emit):
        control['calls'].append(request)
        emit({'event': 'refresh_started', 'run_id': RUN})
        return control['runner'](request, root, emit)
    jobs = refresh_jobs.RefreshJobs(runner=runner, output_dir=snapshot)
    start, get = jobs.start, jobs.get
    def admission(request):
        control['admitted'].append(True)
        return start(request)
    def lookup(job_id):
        control['lookups'].append(job_id)
        return get(job_id)
    monkeypatch.setattr(jobs, 'start', admission)
    monkeypatch.setattr(jobs, 'get', lookup)
    monkeypatch.setattr(main, 'RefreshJobs', lambda: jobs)
    monkeypatch.setattr(datasets, 'open_analytics', lambda: analytics.open_analytics(snapshot))
    monkeypatch.setattr(query, 'open_analytics', lambda: analytics.open_analytics(snapshot))
    app = main.app
    app.dependency_overrides[auth.get_auth_settings] = lambda: settings
    with TestClient(app) as client:
        try:
            yield client, control
        finally:
            control['release'].set()  # Never leave a test worker blocked on client shutdown.
    app.dependency_overrides.clear()


def finish(client, settings, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get('/refresh/' + job_id, headers=headers(settings))
        assert response.status_code == 200
        result = response.json()
        if result['status'] in ('succeeded', 'failed'):
            return result
        time.sleep(0.005)
    pytest.fail('Background job did not finish')


@pytest.mark.parametrize('role,status', [(None, 401), ('viewer', 403), ('analyst', 403)])
def test_start_and_status_authorization_precedes_job_access(environment, settings, role, status):
    client, control = environment
    credential = headers(settings, role) if role else {}
    for response in [client.post('/refresh', json=BODY, headers=credential),
                     client.get('/refresh/unknown', headers=credential)]:
        assert response.status_code == status
    assert not control['admitted'] and not control['lookups'] and not control['calls']


def test_invalid_token_cannot_start_or_poll(environment):
    client, control = environment
    credential = {'Authorization': 'Bearer invalid'}
    assert client.post('/refresh', json=BODY, headers=credential).status_code == 401
    assert client.get('/refresh/anything', headers=credential).status_code == 401
    assert client.get('/refresh/active', headers=credential).status_code == 401
    assert not control['admitted'] and not control['lookups'] and not control['calls']


def test_successful_job_reports_only_public_fields(environment, settings):
    client, control = environment
    marker = secrets.token_urlsafe(24)
    route = next(iter(refresh_jobs.ROUTE_NAMES))
    def runner(request, root, emit):
        emit({'event': 'page_complete', 'run_id': RUN, 'route': route, 'records': 3,
              'reason': marker, 'private_path': str(root)})
        return success()
    control['runner'] = runner
    response = client.post('/refresh', json=BODY, headers=headers(settings))
    assert response.status_code == 202 and response.json()['status'] == 'pending'
    job_id = response.json()['job_id']
    assert response.headers['location'] == '/refresh/' + job_id
    assert response.headers['cache-control'] == 'no-store'
    result = finish(client, settings, job_id)
    assert result['status'] == 'succeeded' and result['outcome'] == 'published' and result['published'] is True
    assert result['row_counts'] == dict(zip(analytics.VIEWS, (3, 6, 9)))
    assert result['fetched_rows'] == {refresh_jobs.ROUTE_NAMES[route]: 3}
    assert result['created_at'] <= result['started_at'] <= result['finished_at']
    assert result['error'] is None and result['warning'] is None
    assert marker not in json.dumps(result) and RUN not in json.dumps(result) and '/runs/' not in json.dumps(result)


@pytest.mark.parametrize('exception', [RuntimeError, OSError, refresh_jobs.pipeline.Error, KeyboardInterrupt])
def test_failed_jobs_keep_safe_errors_and_partial_fetch_counts(environment, settings, snapshot, exception):
    client, control = environment
    before = (snapshot / 'current.json').read_bytes()
    marker = secrets.token_urlsafe(24)
    route = next(iter(refresh_jobs.ROUTE_NAMES))
    def runner(request, root, emit):
        emit({'event': 'page_complete', 'route': route, 'records': 55})
        emit({'event': 'refresh_failed', 'run_id': RUN, 'published': False, 'reason': marker})
        raise exception('/private/secret?api_key=' + marker)
    control['runner'] = runner
    accepted = client.post('/refresh', json=BODY, headers=headers(settings)).json()
    result = finish(client, settings, accepted['job_id'])
    assert result['status'] == 'failed' and result['published'] is False and result['outcome'] is None
    assert result['row_counts'] == {} and list(result['fetched_rows'].values()) == [55]
    assert result['finished_at'] is not None
    assert marker not in json.dumps(result) and '/private/' not in json.dumps(result)
    assert (snapshot / 'current.json').read_bytes() == before


def test_missing_credentials_are_a_predictable_failed_job(environment, settings):
    client, control = environment
    def missing(*args):
        raise refresh_jobs.MissingCredentials
    control['runner'] = missing
    accepted = client.post('/refresh', json=BODY, headers=headers(settings)).json()
    result = finish(client, settings, accepted['job_id'])
    assert result['status'] == 'failed' and result['published'] is False
    assert result['error'] == 'EIA credentials are not configured.'


def test_no_op_is_success_without_new_publication(environment, settings):
    client, control = environment
    control['runner'] = lambda *args: {'status': 'no_op', 'routes': {
        route: {'merged_rows': 3} for route in refresh_jobs.ROUTE_NAMES}}
    accepted = client.post('/refresh', json={**BODY, 'overlap_days': 0}, headers=headers(settings)).json()
    result = finish(client, settings, accepted['job_id'])
    assert result['status'] == 'succeeded' and result['outcome'] == 'no_op' and result['published'] is False
    assert set(result['row_counts'].values()) == {3}


def blocking_runner(control):
    def runner(*args):
        control['entered'].set()
        assert control['release'].wait(5)
        return success()
    return runner


def test_duplicate_start_rejected_and_reads_remain_available(environment, settings):
    client, control = environment
    control['runner'] = blocking_runner(control)
    first = client.post('/refresh', json=BODY, headers=headers(settings))
    assert first.status_code == 202 and control['entered'].wait(2)
    second = client.post('/refresh', json=BODY, headers=headers(settings))
    assert second.status_code == 409 and len(control['calls']) == 1
    assert client.get(first.headers['location'], headers=headers(settings)).json()['status'] == 'running'
    assert client.get('/datasets', headers=headers(settings, 'viewer')).status_code == 200
    preview = client.get('/datasets/national_outages/preview', headers=headers(settings, 'viewer'))
    assert preview.status_code == 200 and preview.json()['total'] == 3
    sql = client.post('/query', json={'sql': 'SELECT * FROM national_outages LIMIT 1'}, headers=headers(settings, 'analyst'))
    assert sql.status_code == 200 and sql.json()['row_count'] == 1
    control['release'].set()
    finish(client, settings, first.json()['job_id'])
    assert client.post('/refresh', json=BODY, headers=headers(settings)).status_code == 202


def test_simultaneous_requests_admit_exactly_one_job(environment, settings):
    client, control = environment
    control['runner'] = blocking_runner(control)
    barrier = Barrier(2)
    def start():
        barrier.wait(timeout=2)
        return client.post('/refresh', json=BODY, headers=headers(settings))
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: start(), range(2)))
    assert sorted(r.status_code for r in responses) == [202, 409]
    assert control['entered'].wait(2) and len(control['calls']) == 1
    control['release'].set()
    finish(client, settings, next(r.json()['job_id'] for r in responses if r.status_code == 202))


@pytest.mark.parametrize('body', [{}, {'end_date': 'bad'}, {**BODY, 'start_date': '2026-01-04'},
                                 {**BODY, 'overlap_days': -1}, {**BODY, 'overlap_days': 3661},
                                 {**BODY, 'conflict_policy': 'ignore'}, {**BODY, 'output_dir': '/private'}])
def test_invalid_requests_do_not_admit_work(environment, settings, body):
    client, control = environment
    assert client.post('/refresh', json=body, headers=headers(settings)).status_code == 400
    assert not control['admitted'] and not control['calls']


def test_unknown_job_status_is_safe_404(environment, settings):
    client, control = environment
    response = client.get('/refresh/not-a-job', headers=headers(settings))
    assert response.status_code == 404 and response.json() == {'detail': 'Refresh job not found.'}
    assert not control['calls']


def test_exception_after_publication_is_recovered_as_success(environment, settings):
    client, control = environment
    def runner(request, root, emit):
        publish(root, version=RUN)
        emit({'event': 'refresh_failed', 'run_id': RUN, 'published': True})
        raise OSError('/private/path')
    control['runner'] = runner
    accepted = client.post('/refresh', json={'end_date': '2026-10-19'}, headers=headers(settings)).json()
    result = finish(client, settings, accepted['job_id'])
    assert result['status'] == 'succeeded' and result['published'] is True
    assert result['warning'] and result['error'] is None
    assert result['row_counts'] == {'national_outages': 2, 'facility_outages': 4, 'generator_outages': 6}


def test_orphan_manifest_does_not_prove_publication(environment, settings, snapshot, tmp_path):
    client, control = environment
    before = (snapshot / 'current.json').read_bytes()
    other_root = tmp_path / 'unpublished'
    orphan = publish(other_root, version=RUN)
    shutil.move(orphan, snapshot / 'runs' / RUN)
    def runner(request, root, emit):
        emit({'event': 'refresh_failed', 'run_id': RUN, 'published': False})
        raise OSError('/private/path')
    control['runner'] = runner
    accepted = client.post('/refresh', json=BODY, headers=headers(settings)).json()
    result = finish(client, settings, accepted['job_id'])
    assert result['status'] == 'failed' and result['published'] is False
    assert (snapshot / 'current.json').read_bytes() == before


def test_unverified_publication_is_not_claimed_as_success(environment, settings):
    client, control = environment
    def runner(request, root, emit):
        emit({'event': 'refresh_failed', 'run_id': RUN, 'published': True})
        raise OSError('/private/path')
    control['runner'] = runner
    accepted = client.post('/refresh', json=BODY, headers=headers(settings)).json()
    result = finish(client, settings, accepted['job_id'])
    assert result['status'] == 'failed' and result['published'] is None and result['warning']


def test_adapter_reuses_engine_options_and_audits_missing_credentials(tmp_path, monkeypatch):
    calls = []
    marker = secrets.token_urlsafe(24)
    class Client:
        def __init__(self, **options):
            assert options['timeout'] == 30 and not options['follow_redirects']
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    monkeypatch.setattr(refresh_jobs.httpx, 'Client', Client)
    def engine(client, key, end, root, **options):
        calls.append((key, end, root, options))
        if not key:
            raise refresh_jobs.pipeline.Error('Missing EIA_API_KEY.')
        return success()
    monkeypatch.setattr(refresh_jobs.pipeline, 'refresh', engine)
    monkeypatch.setattr(refresh_jobs, 'load_api_key', lambda _: marker)
    request = refresh_jobs.RefreshRequest(end_date='2026-01-03', start_date='2026-01-01',
                                          overlap_days=2, conflict_policy='replace')
    assert refresh_jobs.run_refresh(request, tmp_path, lambda _: None)['schema_version'] == 2
    assert calls[0][0] == marker and calls[0][2] == tmp_path
    assert calls[0][3]['start'] == request.start_date and calls[0][3]['overlap_days'] == 2
    assert calls[0][3]['conflict_policy'] == 'replace'
    def missing(_):
        raise refresh_jobs.ExplorationError('Missing key.')
    monkeypatch.setattr(refresh_jobs, 'load_api_key', missing)
    with pytest.raises(refresh_jobs.MissingCredentials):
        refresh_jobs.run_refresh(request, tmp_path, lambda _: None)
    assert len(calls) == 2 and calls[-1][0] == ''  # Engine still owns durable failure auditing.


def test_incomplete_return_value_is_not_success(environment, settings):
    client, control = environment
    control['runner'] = lambda *args: {'schema_version': 2, 'ingestion_run': {'status': 'published'}, 'routes': {}}
    accepted = client.post('/refresh', json=BODY, headers=headers(settings)).json()
    result = finish(client, settings, accepted['job_id'])
    assert result['status'] == 'failed' and result['row_counts'] == {}


def test_shutdown_rejects_new_admission_and_fresh_manager_has_no_old_jobs(tmp_path):
    manager = refresh_jobs.RefreshJobs(runner=lambda *args: success(), output_dir=tmp_path)
    job = manager.start(refresh_jobs.RefreshRequest(end_date='2026-01-03'))
    manager.close()  # Graceful shutdown waits for the admitted job.
    completed = manager.get(job.job_id)
    assert completed.status == 'succeeded' and completed.finished_at is not None
    with pytest.raises(RuntimeError):
        manager.start(refresh_jobs.RefreshRequest(end_date='2026-01-03'))
    restarted = refresh_jobs.RefreshJobs(runner=lambda *args: success(), output_dir=tmp_path)
    try:
        with pytest.raises(KeyError):
            restarted.get(job.job_id)
    finally:
        restarted.close()


@pytest.mark.parametrize('role,status', [(None, 401), ('viewer', 403), ('analyst', 403)])
def test_active_job_authorization_precedes_lookup(environment, settings, monkeypatch, role, status):
    client, control = environment
    monkeypatch.setattr(refresh_jobs.RefreshJobs, 'active', lambda _: pytest.fail('Unauthorized lookup'))
    assert client.get('/refresh/active', headers=headers(settings, role) if role else {}).status_code == status
    assert not control['calls']


def test_active_lookup_returns_only_current_job_and_keeps_conflict(environment, settings):
    client, control = environment
    credential = headers(settings)
    assert client.get('/refresh/active', headers=credential).json() is None
    def blocked(*_):
        control['entered'].set()
        assert control['release'].wait(3)
        return success()
    control['runner'] = blocked
    admitted = client.post('/refresh', json=BODY, headers=credential).json()
    assert control['entered'].wait(2)
    response = client.get('/refresh/active', headers=credential)
    assert response.status_code == 200 and response.json()['job_id'] == admitted['job_id']
    assert response.headers['cache-control'] == 'no-store'
    assert client.post('/refresh', json=BODY, headers=credential).status_code == 409
    control['release'].set()
    assert finish(client, settings, admitted['job_id'])['status'] == 'succeeded'
    assert client.get('/refresh/active', headers=credential).json() is None
