"""FastAPI application entry point."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from outage_explorer.api.health import router as health_router
from outage_explorer.api.auth import router as auth_router
from outage_explorer.api.datasets import router as datasets_router
from outage_explorer.api.query import router as query_router

app = FastAPI(title="Outage Explorer", version="0.1.0")
app.include_router(health_router)

app.include_router(auth_router)
app.include_router(datasets_router)
app.include_router(query_router)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    # FastAPI's default validation errors can echo a submitted password/token.
    status = 400 if request.url.path in ("/datasets", "/query") or request.url.path.startswith("/datasets/") else 422
    return JSONResponse(status_code=status, content={"detail": "Invalid request."})
