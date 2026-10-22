"""An app-owned data opener, invoked only after route authorization/SQL checks."""
from collections.abc import Callable

import duckdb
from fastapi import Request


def open_data(request: Request, production_opener: Callable[[], duckdb.DuckDBPyConnection]):
    # Only trusted application startup sets this attribute. No request parameter
    # or environment flag can redirect the production app into demonstration data.
    opener = getattr(request.app.state, 'analytics_opener', production_opener)
    return opener()
