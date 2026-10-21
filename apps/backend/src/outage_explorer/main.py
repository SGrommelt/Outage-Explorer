"""FastAPI application entry point."""

from fastapi import FastAPI

from outage_explorer.api.health import router as health_router

app = FastAPI(title="Outage Explorer", version="0.1.0")
app.include_router(health_router)
