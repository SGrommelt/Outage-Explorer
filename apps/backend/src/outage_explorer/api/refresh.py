"""Admin-only asynchronous refresh admission and in-memory status polling."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from outage_explorer.api.datasets import _response
from outage_explorer.auth import User, require_refresh
from outage_explorer.refresh_jobs import RefreshBusyError, RefreshJob, RefreshJobs, RefreshRequest

router = APIRouter(prefix='/refresh', tags=['refresh'])


def admin_jobs(request: Request, user: User = Depends(require_refresh)) -> RefreshJobs:
    return request.app.state.refresh_jobs


@router.post('', status_code=202, response_model=RefreshJob)
def start_refresh(body: RefreshRequest, jobs: RefreshJobs = Depends(admin_jobs)) -> JSONResponse:
    try:
        job = jobs.start(body)
    except RefreshBusyError:
        raise HTTPException(409, 'A refresh job is already pending or running.') from None
    except RuntimeError:
        raise HTTPException(503, 'Refresh service is unavailable.') from None
    response = _response(job)
    response.status_code = 202
    response.headers['Location'] = f'/refresh/{job.job_id}'
    return response


@router.get('/active', response_model=RefreshJob | None)
def active_refresh(jobs: RefreshJobs = Depends(admin_jobs)) -> JSONResponse:
    job = jobs.active()
    if job is None:
        return JSONResponse(None, headers={'Cache-Control': 'no-store', 'Vary': 'Authorization'})
    return _response(job)


@router.get('/{job_id}', response_model=RefreshJob)
def refresh_status(job_id: str, jobs: RefreshJobs = Depends(admin_jobs)) -> JSONResponse:
    try:
        return _response(jobs.get(job_id))
    except KeyError:
        raise HTTPException(404, 'Refresh job not found.') from None
