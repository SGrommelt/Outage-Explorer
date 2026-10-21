"""US-08 daily fleet offline share and supporting capacity-MW calculations."""

from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN, localcontext

import duckdb

from outage_explorer.diagnostics import _rows, _window


def daily_fleet_offline_share(db: duckdb.DuckDBPyConnection, start: date, end: date) -> list[dict]:
    """US-08: national outage / national capacity * 100, in percentage points.

    Return exactly one row per requested day. Percentages use Decimal arithmetic
    and explicit half-even rounding to six fractional digits, never binary floats.
    Zero capacity has an undefined share. Ambiguous daily totals are withheld.
    """
    daily = {}
    for row in offline_capacity(db, start, end):
        day = row['outage_date']
        if day in daily:
            continue  # All rows for a duplicate day already have duplicate_key status.
        status = row['status']
        capacity = row['capacity_mw'] if status not in ('duplicate_key', 'unsupported_values') else None
        share = None
        if status == 'ok':
            with localcontext() as context:
                context.prec = 60
                share = (row['offline_capacity_mw'] / capacity * Decimal(100)).quantize(
                    Decimal('0.000001'), rounding=ROUND_HALF_EVEN)
        daily[day] = {
            'outage_date': day,
            'total_fleet_capacity_mw': capacity,
            'offline_capacity_mw': row['offline_capacity_mw'],
            'fleet_offline_share_pct': share,
            'status': status,
        }
    return list(daily.values())


def offline_capacity(db: duckdb.DuckDBPyConnection, start: date, end: date,
                     *, level: str = 'national') -> list[dict]:
    """Query a pinned analytical connection without inferring fully offline units.

    offline_capacity_mw = reported outage_mw
    available_capacity_mw = capacity_mw - outage_mw

    Both metrics are withheld for missing, ambiguous, or unsupported inputs.
    Bounds are an explicit interpretation policy, not universal EIA constraints.
    """
    parameters = _window(start, end)
    if level not in ('national', 'facility', 'generator'):
        raise ValueError("level must be national, facility, or generator.")
    parameters['level'] = level
    facility = 'facility_id' if level != 'national' else 'NULL::VARCHAR AS facility_id'
    generator = 'generator_id' if level == 'generator' else 'NULL::VARCHAR AS generator_id'
    return _rows(db, f"""
        WITH observations AS (
            SELECT outage_date, {facility}, {generator}, capacity_mw, outage_mw
            FROM {level}_outages WHERE outage_date BETWEEN $start AND $end
        ), counted AS (
            SELECT *, count(*) OVER (
                PARTITION BY outage_date, facility_id, generator_id) AS key_count
            FROM observations
        ), evaluated AS (
            SELECT CAST(day AS DATE) AS outage_date, facility_id, generator_id,
                   capacity_mw, outage_mw,
                   CASE WHEN o.outage_date IS NULL THEN 'missing_date'
                        WHEN key_count > 1 THEN 'duplicate_key'
                        WHEN capacity_mw IS NULL OR outage_mw IS NULL THEN 'missing_values'
                        WHEN NOT isfinite(capacity_mw) OR NOT isfinite(outage_mw)
                             OR capacity_mw < 0 OR outage_mw < 0 THEN 'unsupported_values'
                        WHEN outage_mw > capacity_mw THEN 'inconsistent_values'
                        WHEN capacity_mw = 0 THEN 'zero_capacity'
                        ELSE 'ok' END AS status
            FROM generate_series($start::TIMESTAMP, $end::TIMESTAMP, INTERVAL 1 DAY) dates(day)
            LEFT JOIN counted o ON o.outage_date = dates.day
        )
        SELECT $level AS level, *,
               CASE WHEN status IN ('ok', 'zero_capacity') THEN outage_mw END AS offline_capacity_mw,
               CASE WHEN status IN ('ok', 'zero_capacity')
                    THEN capacity_mw - outage_mw END AS available_capacity_mw
        FROM evaluated ORDER BY outage_date, facility_id, generator_id
    """, parameters)
