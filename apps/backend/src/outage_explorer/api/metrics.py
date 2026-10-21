"""Bounded national-only US-08 metric over a published, pinned snapshot."""

from datetime import date
from decimal import Decimal
from typing import Literal

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from outage_explorer.analytics import AnalyticsError, open_analytics
from outage_explorer.api.datasets import _response, _unavailable
from outage_explorer.auth import User, require_dataset
from outage_explorer.capacity import daily_fleet_offline_share

router = APIRouter(prefix='/metrics', tags=['metrics'])


class FleetDay(BaseModel):
    outage_date: date
    total_fleet_capacity_mw: Decimal | None
    offline_capacity_mw: Decimal | None
    fleet_offline_share_pct: Decimal | None
    status: Literal['ok', 'missing_date', 'duplicate_key', 'missing_values',
                    'unsupported_values', 'inconsistent_values', 'zero_capacity']


class FleetResponse(BaseModel):
    rows: list[FleetDay]


@router.get('/fleet-offline-share', response_model=FleetResponse)
def fleet_offline_share(start_date: date = Query(), end_date: date = Query(),
                        user: User = Depends(require_dataset('national_outages'))):
    if start_date > end_date or (end_date - start_date).days >= 366:
        raise HTTPException(400, 'Use an inclusive date window of at most 366 days.')
    try:
        with open_analytics() as db:
            rows = daily_fleet_offline_share(db, start_date, end_date)
    except (AnalyticsError, duckdb.Error, OSError):
        raise _unavailable() from None
    return _response(FleetResponse(rows=rows))
