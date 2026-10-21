"""Offline authentication and capability boundaries, before any data work."""

from dataclasses import replace
import json
import secrets

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pytest

from outage_explorer import auth
from outage_explorer.main import app
from outage_explorer.seed_auth import seed


@pytest.fixture(scope='module')
def credentials():
    passwords = {role.value: secrets.token_urlsafe(24) for role in auth.Role}
    settings = auth.AuthSettings(secrets.token_bytes(32), {
        role.value: auth.Account(auth.User(username=role.value, role=role), auth.hash_password(passwords[role.value]))
        for role in auth.Role})
    return passwords, settings


@pytest.fixture
def client(credentials):
    _, settings = credentials
    app.dependency_overrides[auth.get_auth_settings] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def headers(client, credentials, role):
    passwords, _ = credentials
    response = client.post('/auth/login', json={'username': role, 'password': passwords[role]})
    assert response.status_code == 200
    return {'Authorization': f"Bearer {response.json()['access_token']}"}


@pytest.mark.parametrize('role', list(auth.Role))
def test_login_me_and_role_privacy(client, credentials, role):
    passwords, settings = credentials
    response = client.post('/auth/login', json={'username': role, 'password': passwords[role]})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['expires_in'] == 600 and response.json()['token_type'] == 'bearer'
    assert passwords[role] not in response.text and settings.accounts[role].password_hash not in response.text
    me = client.get('/auth/me', headers={'Authorization': 'Bearer ' + response.json()['access_token']})
    assert me.status_code == 200 and me.json() == {'username': role, 'role': role}
    assert me.headers['cache-control'] == 'no-store'


def test_unknown_user_and_wrong_password_have_identical_safe_failures(client, credentials):
    wrong = secrets.token_urlsafe(20)
    responses = [client.post('/auth/login', json={'username': username, 'password': wrong})
                 for username in ['viewer', 'not-a-user']]
    assert all(r.status_code == 401 and r.headers['www-authenticate'] == 'Bearer' for r in responses)
    assert responses[0].json() == responses[1].json()
    assert all(wrong not in r.text for r in responses)


@pytest.mark.parametrize('payload', [None, {}, {'username': 'viewer'}, {'username': []}, {'password': 'x' * 1025}])
def test_validation_errors_never_echo_credentials(client, payload):
    marker = secrets.token_urlsafe(24)
    if isinstance(payload, dict):
        payload = {**payload, 'sensitive_marker': marker}
    response = client.post('/auth/login', json=payload)
    assert response.status_code == 422
    assert response.json() == {'detail': 'Invalid request.'} and marker not in response.text


@pytest.mark.parametrize('authorization', [None, 'Basic abc', 'Bearer malformed', 'Bearer ' + 'x' * 5000,
                                         'Bearer v1.a.' + 'a' * 43])
def test_missing_invalid_and_malformed_tokens_are_401(client, authorization):
    response = client.get('/auth/me', headers={'Authorization': authorization} if authorization else {})
    assert response.status_code == 401 and response.headers['www-authenticate'] == 'Bearer'
    assert response.json() == {'detail': 'Invalid authentication credentials.'}


def test_tampered_expired_future_wrong_key_and_unknown_user_tokens(client, credentials, monkeypatch):
    _, settings = credentials
    base = 2_000_000_000
    monkeypatch.setattr(auth.time, 'time', lambda: base)
    token = auth.issue_token(settings.accounts['viewer'].user, settings)
    body = token.split('.')
    body[1] = ('a' if body[1][0] != 'a' else 'b') + body[1][1:]
    bad = ['.'.join(body), auth.issue_token(auth.User(username='removed', role=auth.Role.ADMIN), settings),
           auth.issue_token(settings.accounts['admin'].user, replace(settings, signing_key=secrets.token_bytes(32)))]
    for value in bad:
        assert client.get('/auth/me', headers={'Authorization': 'Bearer ' + value}).status_code == 401
    monkeypatch.setattr(auth.time, 'time', lambda: base - 1)
    assert client.get('/auth/me', headers={'Authorization': 'Bearer ' + token}).status_code == 401
    monkeypatch.setattr(auth.time, 'time', lambda: base + settings.token_seconds)
    assert client.get('/auth/me', headers={'Authorization': 'Bearer ' + token}).status_code == 401


def test_roles_are_resolved_from_current_server_settings(client, credentials):
    _, settings = credentials
    token = auth.issue_token(settings.accounts['admin'].user, settings)
    changed = {**settings.accounts, 'admin': auth.Account(auth.User(username='admin', role=auth.Role.VIEWER),
                                                       settings.accounts['admin'].password_hash)}
    app.dependency_overrides[auth.get_auth_settings] = lambda: replace(settings, accounts=changed)
    response = client.get('/auth/me', headers={'Authorization': 'Bearer ' + token})
    assert response.json()['role'] == 'viewer'


@pytest.mark.parametrize('role', list(auth.Role))
def test_dataset_sql_refresh_permissions_guard_work_before_it_runs(credentials, role):
    passwords, settings = credentials
    guarded = FastAPI()
    guarded.dependency_overrides[auth.get_auth_settings] = lambda: settings
    executed = []
    for dataset in auth.DATASET_PERMISSIONS:
        def endpoint(user=Depends(auth.require_dataset(dataset))):
            executed.append('data read')
            return {'ok': True}
        guarded.add_api_route('/' + dataset, endpoint)
    for capability, dependency in [('sql', auth.require_sql), ('refresh', auth.require_refresh)]:
        def endpoint(user=Depends(dependency)):
            executed.append('query/refresh')
            return {'ok': True}
        guarded.add_api_route('/' + capability, endpoint)
    token = auth.issue_token(settings.accounts[role].user, settings)
    with TestClient(guarded) as client:
        for path in [*auth.DATASET_PERMISSIONS, 'sql', 'refresh']:
            before = len(executed)
            assert client.get('/' + path).status_code == 401
            assert len(executed) == before
            allowed = (path == 'national_outages' or
                       path in ('facility_outages', 'generator_outages', 'sql') and role != auth.Role.VIEWER or
                       path == 'refresh' and role == auth.Role.ADMIN)
            response = client.get('/' + path, headers={'Authorization': 'Bearer ' + token})
            assert response.status_code == (200 if allowed else 403)
            assert len(executed) == before + int(allowed)
    expected = ('national_outages',) if role == auth.Role.VIEWER else tuple(auth.DATASET_PERMISSIONS)
    assert auth.allowed_datasets(settings.accounts[role].user) == expected
    with pytest.raises(Exception) as exc:
        auth.authorize_datasets(settings.accounts[role].user, ['unknown_dataset'])
    assert exc.value.status_code == 403


def test_combined_dataset_access_cannot_bypass_viewer_policy(credentials):
    _, settings = credentials
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        auth.authorize_datasets(settings.accounts['viewer'].user, ['national_outages', 'generator_outages'])
    assert exc.value.status_code == 403


def test_hash_salt_and_representation_do_not_reveal_secrets(credentials):
    passwords, settings = credentials
    password = passwords['viewer']
    first = settings.accounts['viewer'].password_hash
    second = auth.hash_password(password)
    assert first != second and auth.verify_password(password, first)
    assert password not in first and password not in repr(settings)
    assert not auth.verify_password(password, 'invalid-hash')


def test_seed_and_settings_load_only_hashes_and_preserve_unrelated_config(tmp_path, monkeypatch, credentials):
    passwords, _ = credentials
    env = tmp_path / '.env'
    unrelated = secrets.token_urlsafe(24)
    env.write_text(f'UNRELATED_CONFIG={unrelated}\nAUTH_SIGNING_KEY=\nAUTH_USERS_JSON=\n')
    answers = iter([passwords[role.value] for role in auth.Role for _ in range(2)])
    monkeypatch.setattr('outage_explorer.seed_auth.getpass.getpass', lambda _: next(answers))
    seed(env)
    content = env.read_text()
    assert f'UNRELATED_CONFIG={unrelated}' in content
    assert all(value not in content for value in passwords.values())
    assert env.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(auth, 'DEFAULT_ENV', env)
    for name in ('AUTH_SIGNING_KEY', 'AUTH_USERS_JSON', 'AUTH_TOKEN_SECONDS'):
        monkeypatch.delenv(name, raising=False)
    auth.get_auth_settings.cache_clear()
    loaded = auth.get_auth_settings()
    assert {a.user.role for a in loaded.accounts.values()} == set(auth.Role)
    assert all(auth.verify_password(passwords[name], a.password_hash) for name, a in loaded.accounts.items())
    with pytest.raises(ValueError, match='already configured'):
        seed(env)
    assert env.read_text() == content
    auth.get_auth_settings.cache_clear()


@pytest.mark.parametrize('configuration', ['', '{bad', '[]', '[{}]'])
def test_invalid_configuration_fails_closed_without_details(tmp_path, monkeypatch, configuration):
    from fastapi import HTTPException
    monkeypatch.setattr(auth, 'DEFAULT_ENV', tmp_path / 'absent')
    monkeypatch.setenv('AUTH_SIGNING_KEY', secrets.token_hex(32))
    monkeypatch.setenv('AUTH_USERS_JSON', configuration)
    monkeypatch.setenv('AUTH_TOKEN_SECONDS', '600')
    auth.get_auth_settings.cache_clear()
    with pytest.raises(HTTPException) as exc:
        auth.get_auth_settings()
    assert exc.value.status_code == 503 and exc.value.detail == 'Authentication is not configured.'
    auth.get_auth_settings.cache_clear()


@pytest.mark.parametrize('fault', ['key', 'ttl', 'hash', 'duplicate_username', 'duplicate_role'])
def test_settings_reject_unsafe_or_ambiguous_accounts(tmp_path, monkeypatch, credentials, fault):
    _, settings = credentials
    users = [{'username': a.user.username, 'role': a.user.role, 'password_hash': a.password_hash}
             for a in settings.accounts.values()]
    key, ttl = secrets.token_hex(32), '600'
    if fault == 'key':
        key = ''
    elif fault == 'ttl':
        ttl = '99999'
    elif fault == 'hash':
        users[0]['password_hash'] = secrets.token_urlsafe(20)
    elif fault == 'duplicate_username':
        users[1]['username'] = users[0]['username']
    else:
        users[1]['role'] = users[0]['role']
    monkeypatch.setattr(auth, 'DEFAULT_ENV', tmp_path / 'absent')
    monkeypatch.setenv('AUTH_SIGNING_KEY', key)
    monkeypatch.setenv('AUTH_TOKEN_SECONDS', ttl)
    monkeypatch.setenv('AUTH_USERS_JSON', json.dumps(users))
    auth.get_auth_settings.cache_clear()
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        auth.get_auth_settings()
    assert exc.value.status_code == 503
    auth.get_auth_settings.cache_clear()


def test_seeding_cancelled_confirmation_preserves_file(tmp_path, monkeypatch, credentials):
    env = tmp_path / '.env'
    env.write_text('UNRELATED_CONFIG=preserve\n')
    values = iter([credentials[0]['viewer'], secrets.token_urlsafe(24)])
    monkeypatch.setattr('outage_explorer.seed_auth.getpass.getpass', lambda _: next(values))
    with pytest.raises(ValueError, match='confirmation'):
        seed(env)
    assert env.read_text() == 'UNRELATED_CONFIG=preserve\n'
