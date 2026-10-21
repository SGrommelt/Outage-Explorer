"""Login and current identity only; no analytical or refresh endpoints."""

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field, SecretStr

from outage_explorer.auth import AuthSettings, User, current_user, get_auth_settings, issue_token, unauthorized, verify_password

router = APIRouter(prefix='/auth', tags=['authentication'])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=1024)


class AccessToken(BaseModel):
    access_token: str
    token_type: str = 'bearer'
    expires_in: int


@router.post('/login', response_model=AccessToken)
def login(body: LoginRequest, response: Response, settings: AuthSettings = Depends(get_auth_settings)) -> AccessToken:
    account = settings.accounts.get(body.username)
    # Unknown users pay the same password-verification cost as known users.
    candidate = account or next(iter(settings.accounts.values()))
    verified = verify_password(body.password.get_secret_value(), candidate.password_hash)
    if account is None or not verified:
        raise unauthorized()
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'
    return AccessToken(access_token=issue_token(account.user, settings), expires_in=settings.token_seconds)


@router.get('/me', response_model=User)
def me(response: Response, user: User = Depends(current_user)) -> User:
    response.headers['Cache-Control'] = 'no-store'
    return user
