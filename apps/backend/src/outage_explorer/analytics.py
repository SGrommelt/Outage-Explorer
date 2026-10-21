"""Three grain-preserving DuckDB views over one validated published snapshot."""

from pathlib import Path

import duckdb
import pyarrow as pa

from outage_explorer.extract_eia import ExtractionError
from outage_explorer.refresh_eia import load_snapshot

DEFAULT_SNAPSHOT_ROOT = Path("data/eia/incremental")

# Fixed projections retain native dates, opaque identifiers, and exact decimals.
# No joins, inferred dimensions, casts, or derived measures are introduced here.
VIEWS = {
    "national_outages": ("us-nuclear-outages", ""),
    "facility_outages": ("facility-nuclear-outages", "facility AS facility_id, facilityName AS facility_name,"),
    "generator_outages": ("generator-nuclear-outages", "facility AS facility_id, generator AS generator_id, facilityName AS facility_name,"),
}


class AnalyticsError(Exception):
    """A published dataset cannot safely be opened for analysis."""


def open_analytics(snapshot_root: Path = DEFAULT_SNAPSHOT_ROOT) -> duckdb.DuckDBPyConnection:
    """Return an owned in-memory connection pinned to one published snapshot.

    Close the connection after use. Reopen it to observe a newly published run.
    Validation reuses the ingestion reader; no EIA requests or audit repairs occur.
    Parquet remains the data store; views contain explicit paths, not copied rows.
    """
    root = Path(snapshot_root).resolve()
    if not (root / "current.json").is_file():
        raise AnalyticsError("No published snapshot: current.json is missing from the snapshot root.")
    try:
        # Resolve the pointer exactly once. load_snapshot validates all routes,
        # physical schemas, keys, coverage, counts, and v2 publication metadata.
        version = load_snapshot(root)[2]
    except ExtractionError as exc:
        raise AnalyticsError(f"Invalid published snapshot: {exc}") from None
    except (OSError, ValueError, pa.ArrowException):
        raise AnalyticsError("Published snapshot files could not be read or validated.") from None

    connection = duckdb.connect(":memory:")
    try:
        for name, (route, identifiers) in VIEWS.items():
            path = root / "runs" / version / f"{route}.parquet"
            connection.read_parquet(str(path), hive_partitioning=False).project(
                f"period AS outage_date, {identifiers} capacity AS capacity_mw, "
                "outage AS outage_mw, percentOutage AS percent_outage"
            ).create_view(name)
    except BaseException as exc:
        connection.close()
        if isinstance(exc, duckdb.Error):
            raise AnalyticsError("Unable to create analytical views from the published Parquet files.") from None
        raise
    return connection
