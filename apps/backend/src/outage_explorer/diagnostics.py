"""Explainable diagnostics on an existing, snapshot-pinned analytical connection."""

from datetime import date
from decimal import Decimal

import duckdb

from outage_explorer.extract_eia import ExtractionError, numeric

# UNION ALL preserves each source grain; this is never used to total levels.
OBSERVATIONS = """
    SELECT 'national' AS level, outage_date, NULL::VARCHAR AS facility_id,
           NULL::VARCHAR AS generator_id, capacity_mw, outage_mw, percent_outage
    FROM national_outages
    UNION ALL
    SELECT 'facility', outage_date, facility_id, NULL::VARCHAR,
           capacity_mw, outage_mw, percent_outage FROM facility_outages
    UNION ALL
    SELECT 'generator', outage_date, facility_id, generator_id,
           capacity_mw, outage_mw, percent_outage FROM generator_outages
"""


def _rows(db: duckdb.DuckDBPyConnection, sql: str, parameters=None) -> list[dict]:
    cursor = db.execute(sql, parameters or {})
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _window(start: date, end: date) -> dict:
    if type(start) is not date or type(end) is not date or start > end:
        raise ValueError("Expected an inclusive date window with start <= end.")
    return {"start": start, "end": end}


def reconcile_daily(db: duckdb.DuckDBPyConnection, start: date, end: date) -> list[dict]:
    """Compare daily capacity/outage totals; differences are not proven errors."""
    return _rows(db, """
        WITH totals AS (
            SELECT level, outage_date, sum(capacity_mw) AS capacity_mw,
                   sum(outage_mw) AS outage_mw, count(*) AS records
            FROM (""" + OBSERVATIONS + """)
            WHERE outage_date BETWEEN $start AND $end
            GROUP BY level, outage_date
        ), daily AS (
            SELECT outage_date,
                   max(capacity_mw) FILTER (level = 'national') AS national_capacity_mw,
                   max(capacity_mw) FILTER (level = 'facility') AS facility_capacity_mw,
                   max(capacity_mw) FILTER (level = 'generator') AS generator_capacity_mw,
                   max(outage_mw) FILTER (level = 'national') AS national_outage_mw,
                   max(outage_mw) FILTER (level = 'facility') AS facility_outage_mw,
                   max(outage_mw) FILTER (level = 'generator') AS generator_outage_mw,
                   max(records) FILTER (level = 'facility') AS facility_count,
                   max(records) FILTER (level = 'generator') AS generator_count
            FROM totals GROUP BY outage_date
        )
        SELECT *, facility_outage_mw - national_outage_mw AS facility_minus_national_mw,
                  generator_outage_mw - national_outage_mw AS generator_minus_national_mw,
                  facility_capacity_mw - national_capacity_mw AS facility_capacity_minus_national_mw,
                  generator_capacity_mw - national_capacity_mw AS generator_capacity_minus_national_mw,
                  'source_comparison' AS category,
                  CASE WHEN national_outage_mw IS NULL OR facility_outage_mw IS NULL
                              OR generator_outage_mw IS NULL THEN 'missing_level'
                       WHEN national_outage_mw = facility_outage_mw
                            AND national_outage_mw = generator_outage_mw
                            AND national_capacity_mw = facility_capacity_mw
                            AND national_capacity_mw = generator_capacity_mw THEN 'equal'
                       ELSE 'difference' END AS status
        FROM daily ORDER BY outage_date
    """, _window(start, end))


def reconcile_facilities(db: duckdb.DuckDBPyConnection, start: date, end: date) -> list[dict]:
    """Compare each facility-day with its generator group, retaining unmatched sides."""
    return _rows(db, """
        WITH generators AS (
            SELECT outage_date, facility_id, count(*) AS generator_count,
                   sum(capacity_mw) AS generator_capacity_mw,
                   sum(outage_mw) AS generator_outage_mw
            FROM generator_outages WHERE outage_date BETWEEN $start AND $end
            GROUP BY outage_date, facility_id
        ), facilities AS (
            SELECT * FROM facility_outages WHERE outage_date BETWEEN $start AND $end
        )
        SELECT coalesce(f.outage_date, g.outage_date) AS outage_date,
               coalesce(f.facility_id, g.facility_id) AS facility_id,
               f.capacity_mw AS facility_capacity_mw, g.generator_capacity_mw,
               f.outage_mw AS facility_outage_mw, g.generator_outage_mw, g.generator_count,
               g.generator_outage_mw - f.outage_mw AS generator_minus_facility_mw,
               g.generator_capacity_mw - f.capacity_mw AS generator_capacity_minus_facility_mw,
               'source_comparison' AS category,
               CASE WHEN f.facility_id IS NULL OR g.facility_id IS NULL THEN 'missing_level'
                    WHEN f.outage_mw = g.generator_outage_mw
                         AND f.capacity_mw = g.generator_capacity_mw THEN 'equal'
                    ELSE 'difference' END AS status
        FROM facilities f FULL OUTER JOIN generators g USING (outage_date, facility_id)
        ORDER BY outage_date, facility_id
    """, _window(start, end))


def find_anomalies(db: duckdb.DuckDBPyConnection, start: date, end: date,
                   *, change_threshold_mw: Decimal | None = None) -> dict[str, list[dict]]:
    """Return contract failures separately from range/change candidates for review.

    The caller supplies expected daily coverage. No entity lifecycle is assumed.
    Change detection is opt-in, absolute MW, and only across adjacent calendar days.
    """
    parameters = _window(start, end)
    if change_threshold_mw is not None:
        if not isinstance(change_threshold_mw, Decimal):
            raise ValueError("change_threshold_mw must be a positive, representable Decimal.")
        try:
            threshold = numeric(change_threshold_mw, 'change_threshold_mw')
        except ExtractionError:
            raise ValueError("change_threshold_mw must be a positive, representable Decimal.") from None
        if threshold <= 0:
            raise ValueError("change_threshold_mw must be positive.")

    prefix = 'WITH observations AS (' + OBSERVATIONS + ') '
    invalid = """outage_date IS NULL OR capacity_mw IS NULL OR outage_mw IS NULL
        OR percent_outage IS NULL OR NOT isfinite(capacity_mw) OR NOT isfinite(outage_mw)
        OR NOT isfinite(percent_outage)
        OR (level IN ('facility', 'generator') AND
            (facility_id IS NULL OR trim(facility_id) = '' OR facility_id != trim(facility_id)))
        OR (level = 'generator' AND
            (generator_id IS NULL OR trim(generator_id) = '' OR generator_id != trim(generator_id)))"""
    # Null dates cannot be assigned to a window: report them on every invocation.
    scoped = '(outage_date BETWEEN $start AND $end OR outage_date IS NULL)'
    result = {
        'missing_dates': _rows(db, prefix + """
            SELECT 'data_quality' AS category, level, CAST(day AS DATE) AS outage_date
            FROM generate_series($start::TIMESTAMP, $end::TIMESTAMP, INTERVAL 1 DAY) dates(day)
            CROSS JOIN (VALUES ('national'), ('facility'), ('generator')) levels(level)
            WHERE NOT EXISTS (SELECT 1 FROM observations o
                              WHERE o.level = levels.level AND o.outage_date = dates.day)
            ORDER BY outage_date, level
        """, parameters),
        'invalid_records': _rows(db, prefix + f"""
            SELECT 'data_quality' AS category, * FROM observations
            WHERE {scoped} AND ({invalid})
            ORDER BY outage_date, level, facility_id, generator_id
        """, parameters),
        'duplicate_keys': _rows(db, prefix + """
            SELECT 'data_quality' AS category, level, outage_date, facility_id, generator_id,
                   count(*) AS records
            FROM observations WHERE outage_date BETWEEN $start AND $end
            GROUP BY level, outage_date, facility_id, generator_id HAVING count(*) > 1
            ORDER BY outage_date, level, facility_id, generator_id
        """, parameters),
        'range_flags': _rows(db, prefix + f"""
            SELECT 'review' AS category, *, capacity_mw < 0 AS negative_capacity,
                   outage_mw < 0 AS negative_outage, outage_mw > capacity_mw AS outage_above_capacity,
                   percent_outage < 0 OR percent_outage > 100 AS percent_outside_range
            FROM observations WHERE outage_date BETWEEN $start AND $end AND NOT ({invalid})
                AND (capacity_mw < 0 OR outage_mw < 0 OR outage_mw > capacity_mw
                     OR percent_outage < 0 OR percent_outage > 100)
            ORDER BY outage_date, level, facility_id, generator_id
        """, parameters),
        'large_changes': [],
    }
    if change_threshold_mw is not None:
        result['large_changes'] = _rows(db, prefix + f"""
            , counted AS (
                SELECT *, count(*) OVER (
                    PARTITION BY level, outage_date, facility_id, generator_id) AS key_count
                FROM observations WHERE outage_date <= $end
            ), changes AS (
                SELECT *, lag(outage_date) OVER series AS previous_date,
                       lag(outage_mw) OVER series AS previous_outage_mw
                FROM counted WHERE key_count = 1 AND NOT ({invalid})
                WINDOW series AS (PARTITION BY level, facility_id, generator_id ORDER BY outage_date)
            )
            SELECT 'review' AS category, level, outage_date, facility_id, generator_id,
                   previous_date, previous_outage_mw, outage_mw,
                   outage_mw - previous_outage_mw AS change_mw, $threshold::DECIMAL(24,6) AS threshold_mw
            FROM changes WHERE outage_date BETWEEN $start AND $end
                AND date_diff('day', previous_date, outage_date) = 1
                AND abs(outage_mw - previous_outage_mw) > $threshold
            ORDER BY outage_date, level, facility_id, generator_id
        """, {**parameters, "threshold": threshold})
    return result
