# Initial setup review — 2026-10-20

## Backend environment

Created `apps/backend/.venv` using Python 3.12.15 and installed the backend as an editable package with its `dev` extra:

```sh
python3.12 -m venv apps/backend/.venv
apps/backend/.venv/bin/python -m pip install -e './apps/backend[dev]'
apps/backend/.venv/bin/python -m pytest apps/backend/tests
apps/backend/.venv/bin/python -m pip check
```

The Python version constraint, setuptools source-package discovery, runtime dependencies, and Pytest extra in `apps/backend/pyproject.toml` all work without changes. All declared runtime modules import successfully. SQLite is available through the standard-library `sqlite3` module (SQLite 3.54.1); no separate SQLite package is needed.

Installed primary versions: FastAPI 0.156.4, Uvicorn 0.54.0, DuckDB 1.5.6, HTTPX 0.28.1, Pandas 2.3.3, PyArrow 23.0.1, Pydantic 2.14.1, SQLGlot 29.0.1, and Pytest 9.1.1.

The backend test passes with one upstream `StarletteDeprecationWarning`: using HTTPX with Starlette's test client is deprecated in favor of HTTPX2. No warning was suppressed and no additional client dependency was introduced. Python dependencies remain bounded ranges rather than a locked environment.

## npm investigation and changes

The initial audit counted 10 affected packages, including dependency ancestors of vulnerable packages. These are not ten independent vulnerabilities. Every affected package belongs to a development dependency tree.

Updated the frontend Vitest requirement from `^3.0.5` to `^4.1.11`. The refreshed lockfile resolves Vitest and `@vitest/mocker` to **4.1.12** and removes Tinypool. This major update is necessary because the advisory states that Vitest 3 will not receive the mocker fix. [Vitest 4's migration guide](https://v4.vitest.dev/guide/migration.html) supports Node 20 and Vite 6; the scaffold uses none of the removed options and its existing tests and configuration pass unchanged. Vite, Tailwind, React, and application code were retained.

| Initially affected package | Relationship | Initial severity | Result and available fix |
| --- | --- | --- | --- |
| `vitest` 3.2.7 | Direct dev dependency | Critical, including inherited Tinypool risk | Updated to 4.1.12; mocker fix starts at 4.1.11; Tinypool removed. |
| `@vitest/mocker` 3.2.7 | Transitive through Vitest | Moderate | Updated to 4.1.12; fixed in 4.1.11. |
| `tinypool` 1.1.1 | Transitive through Vitest | Critical | Removed by Vitest 4; standalone patched release is 2.1.2. No incompatible override was applied to Vitest 3. |
| `tailwindcss` 3.4.19 | Direct dev dependency | High, inherited | Remains; npm proposes Tailwind 4.3.3, requiring a separate major migration. |
| `braces` 3.0.3 | Transitive through Chokidar and Micromatch | High | Remains; no published patch. Tailwind 4 removes this dependency path. |
| `chokidar` 3.6.0 | Transitive through Tailwind | High, inherited | Remains; parent major migration needed to remove this path. |
| `micromatch` 4.0.8 | Transitive through Tailwind/Fast-glob | High, inherited | Remains; parent major migration needed to remove this path. |
| `fast-glob` 3.3.3 | Transitive through Tailwind | High, inherited | Remains; already at the latest compatible version. Audit labels a fix available, but no compatible update removes the vulnerable Braces path. |
| `postcss-selector-parser` 6.1.5 | Transitive through Tailwind/PostCSS Nested | Moderate | Remains; fixed in 7.1.6, outside the parents' declared 6.x ranges. |
| `postcss-nested` 6.2.0 | Transitive through Tailwind | Moderate, inherited | Remains; already at the latest compatible 6.x version. Removing its affected parser requires a major update. |

Ran `npm audit fix` without force using the temporary tools below. It reported the dependency tree up to date and left the same seven findings, confirming that its proposed fixes require a breaking Tailwind update. No `npm audit fix --force`, peer-dependency bypass, or out-of-range transitive override was used. Tailwind remains on 3.4.19 to preserve the current CSS, PostCSS, and shadcn/ui configuration.

### Impact

- The removed [Tinypool vulnerabilities](https://github.com/advisories/GHSA-85c8-ppgw-ccpr) involved worker code execution through prototype pollution under specific caller-controlled options. The initial critical count included both Tinypool and its parent Vitest.
- The fixed [mocker vulnerability](https://github.com/advisories/GHSA-82fw-gwwq-j7x9) allowed local file disclosure through redirect mocks in affected development-server integrations.
- Remaining [Braces risk](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm): deeply nested untrusted glob patterns can exhaust the stack and stop the build/watch process. There is no published Braces patch.
- Remaining [PostCSS selector-parser risk](https://github.com/advisories/GHSA-rj75-hqrm-r3gf): attacker-supplied selectors can cause excessive synchronous CPU use. The advisory says ordinary build-time use on trusted sources is not affected.

The scaffold processes trusted local source/configuration and exposes no user-controlled glob or CSS processing endpoint. These remaining packages run in development/build tooling and are not part of the browser application bundle. This limits current exposure; it does not make the dependency findings resolved. A Tailwind 4 migration is the available parent-level remediation and is deferred to avoid an unnecessary framework migration in this setup task.

### npm resolver issue

npm 10.7 and a temporary npm 10.9.4 both failed dependency resolution with `Cannot read properties of null (reading 'edgesOut')`. A clean temporary workspace also reproduced the failure. This matches the [upstream npm optional-peer resolver issue](https://github.com/npm/cli/issues/9787).

Resolved the update using temporary Node 22 and npm 11 tools:

```sh
npm exec --yes --package=node@22 --package=npm@11 -- npm install
```

This does not add Node/npm as project dependencies or change global installations. The resulting lockfile is used by `npm ci` for normal startup.

## Validation

- Backend Pytest: **1 passed**, 0 failed; one upstream deprecation warning described above.
- Backend `pip check`: **passed**, no broken requirements.
- Runtime imports, including all requested Python packages and SQLite: **passed**.
- Frontend Vitest: **1 passed**, 0 failed.
- Clean install with the existing npm 10.7 and Node 20.15.1 using `npm ci`: **passed** with the updated lockfile.
- `npm run typecheck`: **passed**.
- `npm run build`: **passed**.
- Final `npm audit`: **7 findings — 5 high, 2 moderate, 0 critical**; exits 1 because findings remain.
- `npm audit --omit=dev`: **0 findings**, exits 0.
- Git ignore checks: **passed** for root/nested `.env`, `.venv`, root/nested `node_modules`, `dist`, coverage, TypeScript build metadata, Pytest caches, Python bytecode, and editable-install `.egg-info`. Both `.env.example` files remain eligible for version control. No generated or secret files are tracked.

Initial sandbox network failures were retried with network permission and resolved. No business logic or Git commits were added.
