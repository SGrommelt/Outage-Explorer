"""Single-worker, in-memory refresh jobs wrapping the existing ingestion path."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from outage_explorer.analytics import DEFAULT_SNAPSHOT_ROOT, VIEWS
from outage_explorer import refresh_eia as pipeline
from outage_explorer.extract_eia import SOURCE_CAVEAT
from outage_explorer.explore_eia import DEFAULT_ENV, ExplorationError, load_api_key

ROUTE_NAMES = {route: name for name, (route, _) in VIEWS.items()}


def now() -> datetime:
    return datetime.now(timezone.utc)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    end_date: date
    start_date: date | None = None
    overlap_days: int = Field(7, ge=0, le=3660)
    conflict_policy: Literal['reject', 'replace'] = 'reject'

    @model_validator(mode='after')
    def date_order(self):
        if self.start_date is not None and self.start_date > self.end_date:
            raise ValueError('Invalid date window.')
        return self


class RefreshJob(BaseModel):
    job_id: str
    status: Literal['pending', 'running', 'succeeded', 'failed'] = 'pending'
    request: RefreshRequest
    created_at: datetime = Field(default_factory=now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    fetched_rows: dict[str, int] = Field(default_factory=dict)
    row_counts: dict[str, int] = Field(default_factory=dict)
    published: bool | None = None
    outcome: Literal['published', 'no_op'] | None = None
    error: str | None = None
    warning: str | None = None


@dataclass
class _Record:
    public: RefreshJob
    run_id: str | None = None
    publication_reported: bool | None = None


class RefreshBusyError(Exception):
    pass


class MissingCredentials(Exception):
    pass


def run_refresh(request: RefreshRequest, root: Path, emit) -> dict:
    """Same credentials, client behavior, lock, audits, and publication as the CLI."""
    try:
        key = load_api_key(DEFAULT_ENV)
    except ExplorationError:
        key = ''  # Let the engine persist the failed attempt without networking.
    try:
        with httpx.Client(timeout=30, follow_redirects=False,
                          limits=httpx.Limits(max_keepalive_connections=0)) as client:
            return pipeline.refresh(client, key, request.end_date, root,
                                    start=request.start_date, overlap_days=request.overlap_days,
                                    conflict_policy=request.conflict_policy, emit=emit)
    except Exception:
        if not key:
            raise MissingCredentials from None
        raise


class RefreshJobs:
    """Use one instance per single-worker API process; filesystem lock remains authoritative."""

    def __init__(self, *, runner=None, output_dir: Path = DEFAULT_SNAPSHOT_ROOT):
        self._runner = runner or run_refresh
        self._root = output_dir
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='eia-refresh')
        self._lock = Lock()
        self._jobs: dict[str, _Record] = {}
        self._active: str | None = None
        self._closed = False

    def start(self, request: RefreshRequest) -> RefreshJob:
        with self._lock:
            if self._closed:
                raise RuntimeError('Refresh service is stopping.')
            if self._active is not None:
                raise RefreshBusyError
            job = RefreshJob(job_id=uuid4().hex, request=request.model_copy(deep=True))
            self._jobs[job.job_id] = _Record(job)
            self._active = job.job_id
            pending = job.model_copy(deep=True)
            try:
                self._executor.submit(self._run, job.job_id)
            except RuntimeError:
                del self._jobs[job.job_id]
                self._active = None
                raise
            return pending

    def active(self) -> RefreshJob | None:
        with self._lock:
            return self._jobs[self._active].public.model_copy(deep=True) if self._active else None

    def get(self, job_id: str) -> RefreshJob:
        with self._lock:
            return self._jobs[job_id].public.model_copy(deep=True)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True)  # Graceful stop waits; hard restarts lose job memory.

    def _progress(self, job_id: str, event: dict) -> None:
        with self._lock:
            record = self._jobs[job_id]
            run_id = event.get('run_id')
            if isinstance(run_id, str) and len(run_id) == 32 and all(c in '0123456789abcdef' for c in run_id):
                record.run_id = run_id
            route, count = event.get('route'), event.get('records')
            if event.get('event') == 'page_complete' and route in ROUTE_NAMES and type(count) is int and count >= 0:
                record.public.fetched_rows[ROUTE_NAMES[route]] = count
            if event.get('event') == 'refresh_failed' and type(event.get('published')) is bool:
                record.publication_reported = event['published']
            # No raw event, reason, URL, path, or exception text enters public job state.

    def _published_result(self, run_id: str | None) -> dict | None:
        if run_id is None:
            return None
        try:
            manifest, _, version = pipeline.load_snapshot(self._root)
            if version == run_id and manifest.get('schema_version') == 2:
                return manifest  # Only the validated authoritative pointer proves publication.
        except Exception:
            pass
        return None

    def _success(self, result: dict, *, recovered=False) -> dict:
        no_op = result.get('status') == 'no_op'
        if not no_op and (result.get('schema_version') != 2
                          or result.get('ingestion_run', {}).get('status') != 'published'):
            raise ValueError('Unexpected refresh result.')
        if set(result.get('routes', {})) != set(ROUTE_NAMES):
            raise ValueError('Incomplete refresh result.')
        counts = {}
        for route, name in ROUTE_NAMES.items():
            count = result['routes'][route].get('merged_rows' if no_op else 'records')
            if type(count) is not int or count < 0:
                raise ValueError('Invalid refresh counts.')
            counts[name] = count
        return {'row_counts': counts, 'status': 'succeeded', 'published': not no_op,
                'outcome': 'no_op' if no_op else 'published', 'error': None,
                'warning': ('Snapshot published, but a post-publication operation failed. ' + SOURCE_CAVEAT if recovered
                            else SOURCE_CAVEAT if result.get('validation') else None)}

    def _run(self, job_id: str) -> None:
        record = self._jobs[job_id]
        with self._lock:
            record.public.status = 'running'
            record.public.started_at = now()
        completion = {'status': 'failed', 'error': 'Refresh did not complete; inspect the local ingestion audit.'}
        try:
            result = self._runner(record.public.request, self._root,
                                  lambda event: self._progress(job_id, event))
            completion = self._success(result)
        except (Exception, KeyboardInterrupt) as exc:
            recovered = self._published_result(record.run_id)
            if recovered is not None:
                completion = self._success(recovered, recovered=True)
            else:
                completion = {
                    'status': 'failed',
                    'published': (False if isinstance(exc, MissingCredentials)
                                  or record.publication_reported is False else None),
                    'error': ('EIA credentials are not configured.' if isinstance(exc, MissingCredentials)
                              else 'Refresh did not complete; inspect the local ingestion audit.'),
                    'warning': ('Publication was reported but the current outcome could not be verified.'
                                if record.publication_reported is True else None),
                }
        finally:
            # Pollers must never observe a terminal state without its finish time;
            # a new admission becomes possible in the very same critical section.
            with self._lock:
                record.public = record.public.model_copy(update={**completion, 'finished_at': now()})
                self._active = None
