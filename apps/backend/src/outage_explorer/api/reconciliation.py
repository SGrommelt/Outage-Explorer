"""US-07: authorized, bounded comparisons over one pinned connection per request."""
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from outage_explorer.analytics import AnalyticsError, VIEWS, open_analytics
from outage_explorer.api.data_source import open_data
from outage_explorer.api.datasets import _response, _unavailable
from outage_explorer.auth import Role, User, authorize_datasets, require_roles
from outage_explorer.diagnostics import OBSERVATIONS, find_anomalies, reconcile_daily, reconcile_facilities

router = APIRouter(prefix='/reconciliation', tags=['reconciliation'])
Status = Literal['equal', 'difference', 'missing_level', 'invalid_data']
DESCRIPTIONS = {
    'aggregates_match': 'Numeric aggregates and observed facility keys match. This is internal consistency, not independently verified source completeness.',
    'missing_national_coverage': 'No national observation is available for this requested date.',
    'missing_facility_coverage': 'No facility observations are available for this requested date.',
    'missing_generator_coverage': 'No generator observations are available for this requested date.',
    'missing_facility_key': 'An observed generator group has no matching facility/date record. The reason for the missing record is unknown.',
    'missing_generator_group': 'An observed facility/date has no matching generator group. Coverage and lifecycle reasons are not established.',
    'facility_group_mismatch': 'At least one facility differs from its generator sums. Facility differences can cancel in daily totals; inspect the selected-date comparisons.',
    'capacity_mismatch': 'Reported capacity differs from the independently summed comparison grain; the exact signed gap is retained.',
    'outage_mismatch': 'Reported outage differs from the independently summed comparison grain; the exact signed gap is retained.',
    'invalid_values': 'Required comparison values or identifiers are invalid. Ambiguous aggregates are withheld.',
    'duplicate_keys': 'Duplicate natural keys make this comparison ambiguous. Aggregates are withheld rather than deduplicated.',
    'unsupported_values': 'Values fall outside the supported diagnostic interpretation range. Aggregates are withheld for review; this is not a confirmed source error.',
    'unknown_cause': 'The source cause is unverified. Differences may involve definitions, coverage, reporting timing, or revisions; none is asserted as the cause.',
}


class Reason(BaseModel):
    code: str
    description: str


def explanations(codes):
    return [Reason(code=code, description=DESCRIPTIONS[code]) for code in dict.fromkeys(codes)]


class Completeness(BaseModel):
    mode: Literal['production', 'demo']
    independently_verified: bool = False
    caveat: str


def completeness(request):
    demo = getattr(request.app.state, 'data_mode', 'production') == 'demo'
    return Completeness(mode='demo' if demo else 'production', caveat=(
        'DEMO: saved real EIA data, completeness unverified. The facility API reports inconsistent totals; an empty terminal page and matching aggregates do not resolve that discrepancy. No production publication.'
        if demo else 'Validated published snapshot. Matching numeric aggregates do not independently certify EIA source coverage.'))


def analyst(user: User = Depends(require_roles(Role.ANALYST, Role.ADMIN))):
    authorize_datasets(user, VIEWS)
    return user


class Comparison(BaseModel):
    outage_date: date
    status: Status
    numeric_aggregates_match: bool | None = None
    reasons: list[Reason]


class Daily(Comparison):
    national_capacity_mw: Decimal | None = None
    facility_capacity_mw: Decimal | None = None
    generator_capacity_mw: Decimal | None = None
    national_outage_mw: Decimal | None = None
    facility_outage_mw: Decimal | None = None
    generator_outage_mw: Decimal | None = None
    facility_minus_national_mw: Decimal | None = None
    generator_minus_national_mw: Decimal | None = None
    generator_minus_facility_mw: Decimal | None = None
    facility_capacity_minus_national_mw: Decimal | None = None
    generator_capacity_minus_national_mw: Decimal | None = None
    generator_capacity_minus_facility_mw: Decimal | None = None
    national_count: int = 0
    facility_count: int = 0
    generator_count: int = 0
    missing_facility_keys: int | None = 0
    missing_generator_groups: int | None = 0
    facility_groups_with_differences: int | None = 0


class Facility(Comparison):
    facility_id: str | None
    facility_capacity_mw: Decimal | None = None
    generator_capacity_mw: Decimal | None = None
    facility_outage_mw: Decimal | None = None
    generator_outage_mw: Decimal | None = None
    generator_count: int = 0
    generator_minus_facility_mw: Decimal | None = None
    generator_capacity_minus_facility_mw: Decimal | None = None


class Summary(BaseModel):
    days_requested: int = 0
    days_compared: int = 0
    days_matching: int = 0
    days_with_differences: int = 0
    days_unavailable: int = 0


class DailyResponse(BaseModel):
    start_date: date | None
    end_date: date | None
    available_start: date | None
    available_end: date | None
    source_completeness: Completeness
    summary: Summary
    rows: list[Daily]


class FacilityResponse(BaseModel):
    date: date
    page: int
    page_size: int
    total: int
    source_completeness: Completeness
    rows: list[Facility]


def valid_params(request, allowed):
    if any(key not in allowed or len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise HTTPException(400, 'Invalid reconciliation parameters.')


def bounded(start, end):
    if start and end and (start > end or (end - start).days > 30):
        raise HTTPException(400, 'Use an inclusive date window of at most 31 days.')


def quality(db, start, end):
    checks = find_anomalies(db, start, end)
    return {code: checks[key] for code, key in [('invalid_values', 'invalid_records'),
            ('duplicate_keys', 'duplicate_keys'), ('unsupported_values', 'range_flags')]}


def bad_codes(checks, day, facility=None):
    return [code for code, records in checks.items() if any(
        (row['outage_date'] is None or row['outage_date'] == day)
        and (facility is None or row.get('facility_id') == facility)
        for row in records)]


def measure_codes(row, facility=False):
    codes = []
    if facility:
        if row['facility_capacity_mw'] is None: codes.append('missing_facility_key')
        if row['generator_count'] is None: codes.append('missing_generator_group')
        if row['generator_capacity_minus_facility_mw']: codes.append('capacity_mismatch')
        if row['generator_minus_facility_mw']: codes.append('outage_mismatch')
    else:
        if any(row.get(key) for key in ('facility_capacity_minus_national_mw', 'generator_capacity_minus_national_mw', 'generator_capacity_minus_facility_mw')):
            codes.append('capacity_mismatch')
        if any(row.get(key) for key in ('facility_minus_national_mw', 'generator_minus_national_mw', 'generator_minus_facility_mw')):
            codes.append('outage_mismatch')
    return codes


@router.get('', response_model=DailyResponse)
def daily(request: Request, start_date: date | None = None, end_date: date | None = None,
          user: User = Depends(analyst)):
    valid_params(request, {'start_date', 'end_date'})
    bounded(start_date, end_date)
    try:
        with open_data(request, open_analytics) as db:
            earliest, latest = db.execute('SELECT min(outage_date), max(outage_date) FROM (' + OBSERVATIONS + ')').fetchone()
            start, end = start_date, end_date or latest
            if end is not None and start is None:
                start = end - timedelta(days=min(29, (end - date.min).days))
                if earliest and earliest <= end: start = max(earliest, start)
            bounded(start, end)
            rows = []
            if start is not None and end is not None:
                observed = {row['outage_date']: row for row in reconcile_daily(db, start, end)}
                groups = reconcile_facilities(db, start, end)
                checks = quality(db, start, end)
                counts = {(day, level): count for day, level, count in db.execute(
                    'SELECT outage_date, level, count(*) FROM (' + OBSERVATIONS + ') WHERE outage_date BETWEEN ? AND ? GROUP BY outage_date, level', [start, end]).fetchall()}
                for offset in range((end - start).days + 1):
                    day = start + timedelta(days=offset)
                    row = observed.get(day, {'outage_date': day})
                    codes = bad_codes(checks, day)
                    invalid = bool(codes)
                    for level in ('national', 'facility', 'generator'):
                        row[level + '_count'] = counts.get((day, level), 0)
                        if not row[level + '_count']: codes.append('missing_' + level + '_coverage')
                    day_groups = [group for group in groups if group['outage_date'] == day]
                    row['missing_facility_keys'] = None if invalid else sum(group['facility_capacity_mw'] is None for group in day_groups)
                    row['missing_generator_groups'] = None if invalid else sum(group['generator_count'] is None for group in day_groups)
                    row['facility_groups_with_differences'] = None if invalid else sum(group['status'] == 'difference' for group in day_groups)
                    if row['facility_groups_with_differences']: codes.append('facility_group_mismatch')
                    if row['missing_facility_keys']: codes.append('missing_facility_key')
                    if row['missing_generator_groups']: codes.append('missing_generator_group')
                    differences = []
                    if not invalid:
                        for measure, name in [('outage', 'generator_minus_facility_mw'), ('capacity', 'generator_capacity_minus_facility_mw')]:
                            left, right = row.get('generator_' + measure + '_mw'), row.get('facility_' + measure + '_mw')
                            row[name] = left - right if left is not None and right is not None else None
                        differences = measure_codes(row)
                        codes.extend(differences)
                    if invalid:
                        row = {key: value for key, value in row.items() if not key.endswith('_mw')}
                        status, numeric_match = 'invalid_data', None
                    elif any(row[level + '_count'] == 0 for level in ('national', 'facility', 'generator')):
                        status, numeric_match = 'missing_level', None
                    else:
                        numeric_match = not differences
                        status = 'difference' if codes else 'equal'
                    if status == 'difference' or differences: codes.append('unknown_cause')
                    row.update(status=status, numeric_aggregates_match=numeric_match,
                               reasons=explanations(codes or ['aggregates_match']))
                    rows.append(Daily(**row))
    except (AnalyticsError, duckdb.Error, OSError):
        raise _unavailable() from None
    matching = sum(row.status == 'equal' for row in rows)
    differences = sum(row.status == 'difference' for row in rows)
    return _response(DailyResponse(start_date=start, end_date=end, available_start=earliest, available_end=latest,
        source_completeness=completeness(request), rows=rows, summary=Summary(days_requested=len(rows),
        days_compared=matching + differences, days_matching=matching, days_with_differences=differences,
        days_unavailable=len(rows) - matching - differences)))


@router.get('/facilities', response_model=FacilityResponse)
def facilities(request: Request, selected_date: date = Query(alias='date'), page: int = Query(1, ge=1, le=2_147_483_647),
               page_size: int = Query(25, ge=1, le=100), user: User = Depends(analyst)):
    valid_params(request, {'date', 'page', 'page_size'})
    try:
        with open_data(request, open_analytics) as db:
            groups = reconcile_facilities(db, selected_date, selected_date)
            checks = quality(db, selected_date, selected_date)
            rows = []
            for group in groups[(page - 1) * page_size:page * page_size]:
                codes = bad_codes(checks, selected_date, group['facility_id'])
                if codes:
                    group = {key: value for key, value in group.items() if not key.endswith('_mw')}
                    status, numeric_match = 'invalid_data', None
                else:
                    codes = measure_codes(group, facility=True)
                    status = 'missing_level' if group['status'] == 'missing_level' else 'difference' if codes else 'equal'
                    numeric_match = True if status == 'equal' else False if status == 'difference' else None
                    if status == 'difference': codes.append('unknown_cause')
                group.update(status=status, numeric_aggregates_match=numeric_match,
                             generator_count=group.get('generator_count') or 0,
                             reasons=explanations(codes or ['aggregates_match']))
                rows.append(Facility(**group))
    except (AnalyticsError, duckdb.Error, OSError):
        raise _unavailable() from None
    return _response(FacilityResponse(date=selected_date, page=page, page_size=page_size, total=len(groups),
                                     source_completeness=completeness(request), rows=rows))
