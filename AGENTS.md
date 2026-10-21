# Engineering guidelines

- Keep this monorepo simple: `apps/backend` owns the Python API; `apps/frontend` owns the React UI.
- Target Python 3.12 and strict TypeScript. Use typed Pydantic models at API boundaries.
- Put HTTP routes in `outage_explorer/api`, reusable UI in `src/components`, and small shared frontend helpers in `src/lib`.
- Add domain modules only when real features require them. Do not introduce services, repositories, shared packages, or infrastructure speculatively.
- Use Python's standard-library `sqlite3` for SQLite. Do not add an ORM without a concrete need.
- Use TanStack Query for remote state and TanStack Table for tables when implemented. Load Monaco and charts only where needed.
- Use Tailwind and locally owned shadcn/ui components; keep components accessible and avoid adding a second UI framework.
- Never commit secrets, local databases, generated builds, virtual environments, or node_modules. Keep `.env.example` files synchronized with settings.
- Cover behavior changes with focused tests. Run `npm test`, `npm run build`, and backend `python -m pytest` before handing off changes; report checks that cannot run.
- Update README startup instructions when configuration changes. Keep dependencies minimal and update the npm lockfile with dependency changes.
- Do not commit or publish unless the user requests it.
