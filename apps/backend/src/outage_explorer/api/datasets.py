"""Authenticated catalog and bounded previews over one published snapshot."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from outage_explorer.api.data_source import open_data
from outage_explorer.analytics import AnalyticsError, open_analytics
from outage_explorer.auth import User, allowed_datasets, authorize_datasets, current_user

router = APIRouter(prefix='/datasets', tags=['datasets'])


@dataclass(frozen=True)
class Dataset:
    name: str
    description: str
    query: str
    order: str
    facility_filter: bool = False


# SQL identifiers/order clauses come only from these fixed server-owned values.
DATASETS = {
    'national_outages': Dataset('national_outages', 'Daily reported U.S. nuclear capacity and outages.',
                               'SELECT * FROM national_outages', 'outage_date'),
    'facility_outages': Dataset('facility_outages', 'Daily nuclear capacity and outages per facility.',
                               'SELECT * FROM facility_outages', 'outage_date, facility_id', True),
    'generator_outages': Dataset('generator_outages', 'Daily nuclear capacity and outages per facility and generator.',
                                'SELECT * FROM generator_outages', 'outage_date, facility_id, generator_id', True),
}


class ColumnInfo(BaseModel):
    name: str
    type: str


class DatasetInfo(BaseModel):
    name: str
    description: str
    columns: list[ColumnInfo]


class CatalogResponse(BaseModel):
    datasets: list[DatasetInfo]


class PreviewResponse(BaseModel):
    dataset: str
    page: int
    page_size: int
    total: int
    rows: list[dict[str, date | Decimal | str | None]]


def authorized_dataset(dataset_name: str, user: User = Depends(current_user)) -> Dataset:
    dataset = DATASETS.get(dataset_name)
    if dataset is None:
        raise HTTPException(404, 'Dataset not found.')
    authorize_datasets(user, (dataset.name,))
    return dataset


def _response(payload: BaseModel) -> JSONResponse:
    return JSONResponse(payload.model_dump(mode="json"), headers={'Cache-Control': 'no-store', 'Vary': 'Authorization'})


def _unavailable() -> HTTPException:
    return HTTPException(503, 'Published data is unavailable.')


@router.get('', response_model=CatalogResponse)
def catalog(request: Request, user: User = Depends(current_user)) -> JSONResponse:
    names = allowed_datasets(user)
    authorize_datasets(user, names)  # Complete authorization before snapshot work.
    try:
        with open_data(request, open_analytics) as db:
            datasets = []
            for name in names:
                spec = DATASETS[name]
                columns = db.execute(spec.query + ' LIMIT 0').description
                datasets.append({'name': name, 'description': getattr(request.app.state, 'dataset_notice', '') + spec.description,
                                 'columns': [{'name': c[0], 'type': str(c[1])} for c in columns]})
    except (AnalyticsError, duckdb.Error, OSError):
        raise _unavailable() from None
    return _response(CatalogResponse(datasets=datasets))


@router.get('/{dataset_name}/preview', response_model=PreviewResponse)
def preview(request: Request, dataset: Dataset = Depends(authorized_dataset),
            start_date: str | None = Query(None, pattern=r'^\d{4}-\d{2}-\d{2}$'),
            end_date: str | None = Query(None, pattern=r'^\d{4}-\d{2}-\d{2}$'),
            facility_id: str | None = Query(None, min_length=1, max_length=256),
            page: int = Query(1, ge=1, le=2_147_483_647),
            page_size: int = Query(50, ge=1, le=200)) -> JSONResponse:
    allowed = {'start_date', 'end_date', 'facility_id', 'page', 'page_size'}
    if any(key not in allowed or len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise HTTPException(400, 'Invalid filters or pagination.')
    try:
        start = date.fromisoformat(start_date) if start_date else None
        end = date.fromisoformat(end_date) if end_date else None
    except ValueError:
        raise HTTPException(400, 'Invalid date range.') from None
    if start and end and start > end:
        raise HTTPException(400, 'Invalid date range.')
    if facility_id is not None:
        if not dataset.facility_filter or not facility_id.strip() or facility_id != facility_id.strip():
            raise HTTPException(400, 'Invalid facility filter for this dataset.')
    parameters = {'start': start, 'end': end}
    where = ' WHERE ($start::DATE IS NULL OR outage_date >= $start::DATE) AND ($end::DATE IS NULL OR outage_date <= $end::DATE)'
    if dataset.facility_filter:
        where += ' AND ($facility_id::VARCHAR IS NULL OR facility_id = $facility_id)'
        parameters['facility_id'] = facility_id
    # Dataset.query/order are fixed allowlist entries, never request strings.
    filtered = dataset.query + where
    try:
        with open_data(request, open_analytics) as db:
            total = db.execute('SELECT count(*) FROM (' + filtered + ')', parameters).fetchone()[0]
            cursor = db.execute(filtered + ' ORDER BY ' + dataset.order + ' LIMIT $limit OFFSET $offset',
                                {**parameters, 'limit': page_size, 'offset': (page - 1) * page_size})
            columns = [c[0] for c in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    except (AnalyticsError, duckdb.Error, OSError):
        raise _unavailable() from None
    return _response(PreviewResponse(dataset=dataset.name, page=page, page_size=page_size,
                                     total=total, rows=rows))
