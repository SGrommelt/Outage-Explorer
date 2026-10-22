"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from outage_explorer.api.health import router as health_router
from outage_explorer.api.auth import router as auth_router
from outage_explorer.api.datasets import router as datasets_router
from outage_explorer.api.query import router as query_router
from outage_explorer.api.refresh import router as refresh_router
from outage_explorer.api.metrics import router as metrics_router
from outage_explorer.api.reconciliation import router as reconciliation_router
from outage_explorer.refresh_jobs import RefreshJobs


@asynccontextmanager
async def lifespan(application: FastAPI):
    jobs = RefreshJobs()
    application.state.refresh_jobs = jobs
    try:
        yield
    finally:
        await run_in_threadpool(jobs.close)


app = FastAPI(title="Outage Explorer", version="0.1.0", lifespan=lifespan)
app.include_router(health_router)

app.include_router(auth_router)
app.include_router(datasets_router)
app.include_router(query_router)
app.include_router(refresh_router)
app.include_router(metrics_router)
app.include_router(reconciliation_router)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    # FastAPI's default validation errors can echo a submitted password/token.
    status = 400 if request.url.path in ("/datasets", "/query", "/refresh", "/reconciliation") or request.url.path.startswith(("/datasets/", "/metrics/", "/reconciliation/")) else 422
    return JSONResponse(status_code=status, content={"detail": "Invalid request."})
