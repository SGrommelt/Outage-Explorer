"""Local authentication and deny-by-default capability/dataset policy."""

import base64
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Iterable

from dotenv import dotenv_values
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from outage_explorer.explore_eia import DEFAULT_ENV

ITERATIONS = 600_000


class Role(StrEnum):
    VIEWER = 'viewer'
    ANALYST = 'analyst'
    ADMIN = 'admin'


class User(BaseModel):
    username: str
    role: Role


@dataclass(frozen=True)
class Account:
    user: User
    password_hash: str = field(repr=False)


@dataclass(frozen=True)
class AuthSettings:
    signing_key: bytes = field(repr=False)
    accounts: dict[str, Account] = field(repr=False)
    token_seconds: int = 600


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 1024:
        raise ValueError('Choose a password of 12–1024 characters.')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, ITERATIONS)
    return f'pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}'


def valid_hash(encoded: str) -> bool:
    return bool(re.fullmatch(r'pbkdf2_sha256\$600000\$[a-f0-9]{32}\$[a-f0-9]{64}', encoded))


def verify_password(password: str, encoded: str) -> bool:
    if not valid_hash(encoded):
        return False
    _, _, salt, expected = encoded.split('$')
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS)
    return hmac.compare_digest(actual, bytes.fromhex(expected))


@lru_cache
def get_auth_settings() -> AuthSettings:
    try:
        values = dotenv_values(DEFAULT_ENV, interpolate=False)
        def setting(name, default=None):
            return os.environ.get(name, values.get(name, default))
        key = setting('AUTH_SIGNING_KEY', '')
        if not isinstance(key, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', key):
            raise ValueError
        ttl = int(setting('AUTH_TOKEN_SECONDS', '600'))
        if not 60 <= ttl <= 900:
            raise ValueError
        configured = json.loads(setting('AUTH_USERS_JSON', ''))
        if not isinstance(configured, list) or len(configured) != 3:
            raise ValueError
        accounts = {}
        for entry in configured:
            username = entry['username']
            role = Role(entry['role'])
            encoded = entry['password_hash']
            if (not isinstance(username, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', username)
                    or username in accounts or not isinstance(encoded, str) or not valid_hash(encoded)):
                raise ValueError
            accounts[username] = Account(User(username=username, role=role), encoded)
        if {a.user.role for a in accounts.values()} != set(Role):
            raise ValueError
        return AuthSettings(bytes.fromhex(key), accounts, ttl)
    except (ValueError, TypeError, KeyError, OSError):
        raise HTTPException(503, 'Authentication is not configured.') from None


def unauthorized() -> HTTPException:
    return HTTPException(401, 'Invalid authentication credentials.', headers={'WWW-Authenticate': 'Bearer'})


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def issue_token(user: User, settings: AuthSettings) -> str:
    issued = int(time.time())
    payload = _encode(json.dumps({'sub': user.username, 'iat': issued,
                                 'exp': issued + settings.token_seconds,
                                 'nonce': secrets.token_hex(16)}, separators=(',', ':')).encode())
    message = f'v1.{payload}'
    signature = hmac.digest(settings.signing_key, message.encode(), 'sha256')
    return f'{message}.{_encode(signature)}'


bearer = HTTPBearer(auto_error=False)


def require_token(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    if credentials is None or credentials.scheme.lower() != 'bearer' or len(credentials.credentials) > 4096:
        raise unauthorized()
    return credentials.credentials


def current_user(token: str = Depends(require_token), settings: AuthSettings = Depends(get_auth_settings)) -> User:
    try:
        if not re.fullmatch(r'v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}', token):
            raise ValueError
        version, payload, signature = token.split('.')
        expected = _encode(hmac.digest(settings.signing_key, f'{version}.{payload}'.encode(), 'sha256'))
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        issued, expires = claims['iat'], claims['exp']
        now = int(time.time())
        if (type(issued) is not int or type(expires) is not int or issued > now
                or expires <= now or not 0 < expires - issued <= settings.token_seconds):
            raise ValueError
        # Roles come from current server configuration, never client token claims.
        return settings.accounts[claims['sub']].user
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise unauthorized() from None


def require_roles(*roles: Role):
    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, 'Forbidden.')
        return user
    return dependency


require_refresh = require_roles(Role.ADMIN)
require_sql = require_roles(Role.ANALYST, Role.ADMIN)
DATASET_PERMISSIONS = {
    'national_outages': frozenset(Role),
    'facility_outages': frozenset((Role.ANALYST, Role.ADMIN)),
    'generator_outages': frozenset((Role.ANALYST, Role.ADMIN)),
}


def allowed_datasets(user: User) -> tuple[str, ...]:
    return tuple(name for name, roles in DATASET_PERMISSIONS.items() if user.role in roles)


def authorize_datasets(user: User, datasets: Iterable[str]) -> None:
    if any(user.role not in DATASET_PERMISSIONS.get(name, ()) for name in datasets):
        raise HTTPException(403, 'Forbidden.')


def require_dataset(dataset: str):
    def dependency(user: User = Depends(current_user)) -> User:
        authorize_datasets(user, (dataset,))
        return user
    return dependency
