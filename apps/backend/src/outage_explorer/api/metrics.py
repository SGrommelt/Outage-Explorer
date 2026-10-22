"""Bounded national-only US-08 metric over a published, pinned snapshot."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from outage_explorer.api.data_source import open_data
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
def fleet_offline_share(request: Request, start_date: date | None = None, end_date: date | None = None,
                        user: User = Depends(require_dataset('national_outages'))):
    if start_date and end_date and (start_date > end_date or (end_date - start_date).days >= 366):
        raise HTTPException(400, 'Use an inclusive date window of at most 366 days.')
    try:
        with open_data(request, open_analytics) as db:
            if start_date is None or end_date is None:
                earliest, latest = db.execute('SELECT min(outage_date), max(outage_date) FROM national_outages').fetchone()
                end_date = end_date or latest
                if start_date is None and end_date is not None:
                    start_date = end_date - timedelta(days=min(29, (end_date - date.min).days))
                    if earliest and earliest <= end_date:
                        start_date = max(start_date, earliest)
            if start_date and end_date and (start_date > end_date or (end_date - start_date).days >= 366):
                raise HTTPException(400, 'Use an inclusive date window of at most 366 days.')
            rows = daily_fleet_offline_share(db, start_date, end_date) if start_date and end_date else []
    except (AnalyticsError, duckdb.Error, OSError):
        raise _unavailable() from None
    return _response(FleetResponse(rows=rows))
