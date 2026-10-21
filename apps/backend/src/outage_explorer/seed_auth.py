"""Interactively configure three development-only personas; never print passwords."""

import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import tempfile

from dotenv import dotenv_values

from outage_explorer.auth import Role, hash_password
from outage_explorer.explore_eia import DEFAULT_ENV


def seed(env_file: Path) -> None:
    existing = dotenv_values(env_file, interpolate=False) if env_file.exists() else {}
    if existing.get('AUTH_SIGNING_KEY') or existing.get('AUTH_USERS_JSON'):
        raise ValueError('Authentication is already configured; refusing to replace it.')
    accounts = []
    for role in Role:
        password = getpass.getpass(f'Development-only {role.value} password (12–1024 characters): ')
        confirm = getpass.getpass('Confirm password: ')
        if password != confirm:
            raise ValueError('Password confirmation did not match; configuration unchanged.')
        accounts.append({'username': role.value, 'role': role.value, 'password_hash': hash_password(password)})
    # Preserve unrelated configuration verbatim, including the EIA key. Nothing
    # from the existing file is logged or included in an exception message.
    content = env_file.read_text() if env_file.exists() else ''
    retained = [line for line in content.splitlines()
                if not line.lstrip().startswith(('AUTH_SIGNING_KEY=', 'AUTH_USERS_JSON=', 'AUTH_TOKEN_SECONDS='))]
    retained.extend([f'AUTH_SIGNING_KEY={secrets.token_hex(32)}', 'AUTH_TOKEN_SECONDS=600',
                     "AUTH_USERS_JSON='" + json.dumps(accounts, separators=(',', ':')) + "'"])
    env_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=env_file.parent, delete=False) as output:
            temporary = Path(output.name)  # tempfile creates mode 0600.
            output.write('\n'.join(retained) + '\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, env_file)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=DEFAULT_ENV)
    args = parser.parse_args()
    try:
        seed(args.env_file)
    except (OSError, ValueError, EOFError, KeyboardInterrupt):
        print('Authentication setup failed or was cancelled; no credentials are displayed.')
        return 1
    print('Development-only viewer, analyst, and admin configured. Restart the API to load settings.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
