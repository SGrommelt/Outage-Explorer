"""Explicit, separate FastAPI demo entry point; no production data fallback."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.concurrency import run_in_threadpool

from outage_explorer.api import auth, datasets, health, metrics, query, refresh, reconciliation
from outage_explorer.demo_data import DEFAULT_DEMO_ROOT, NOTICE, open_demo
from outage_explorer.main import invalid_request
from outage_explorer.refresh_jobs import RefreshJobs


def read_only_refresh(*_):
    raise RuntimeError('Read-only evidence demo.')


class DemoJobs(RefreshJobs):
    """Reuse job admission/lifecycle; every demo job is explicitly non-publishing."""
    def __init__(self, root: Path):
        super().__init__(runner=read_only_refresh, output_dir=root)

    def get(self, job_id):
        result = super().get(job_id)
        if result.status == 'failed':
            return result.model_copy(update={'published': False, 'warning': None,
                'error': 'Read-only evidence demo: refresh is disabled. No EIA request, publication, watermark, or ingestion audit was changed.'})
        return result


def create_demo_app(root: Path = DEFAULT_DEMO_ROOT) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application):
        # Fail startup if preparation was omitted/corrupted; never try production.
        with open_demo(root):
            pass
        jobs = DemoJobs(root)
        application.state.refresh_jobs = jobs
        try:
            yield
        finally:
            await run_in_threadpool(jobs.close)
    application = FastAPI(title='Outage Explorer DEMO — completeness unverified', lifespan=lifespan)
    application.state.analytics_opener = lambda: open_demo(root)
    application.state.data_mode = 'demo'
    application.state.dataset_notice = NOTICE + ' '
    for module in (health, auth, datasets, query, metrics, refresh, reconciliation):
        application.include_router(module.router)
    application.add_exception_handler(RequestValidationError, invalid_request)

    @application.middleware('http')
    async def label_demo(request, call_next):
        response = await call_next(request)
        response.headers['X-Outage-Explorer-Data-Mode'] = 'demo-completeness-unverified'
        response.headers['X-Outage-Explorer-Notice'] = NOTICE
        return response
    return application


app = create_demo_app()
