import { useEffect, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { inputStyle } from "@/lib/data";
import { useSession } from "./session";
import { Button } from "./ui/button";

interface RefreshJob {
  job_id: string; status: "pending" | "running" | "succeeded" | "failed";
  created_at: string; started_at: string | null; finished_at: string | null;
  fetched_rows: Record<string, number>; row_counts: Record<string, number>;
  published: boolean | null; outcome: "published" | "no_op" | null;
  error: string | null; warning: string | null;
}
const active = (job?: RefreshJob) => job?.status === "pending" || job?.status === "running";
function refreshError(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 409) return "A refresh is already pending or running. No new job was started.";
    if (error.status === 403) return "Only Admin can refresh data.";
    if (error.status === 404) return "This job is no longer known to this backend process. Publication outcome is unknown; check the ingestion audit.";
    if (error.status === 400 || error.status === 422) return "Invalid refresh window or overlap. Check the form values.";
  }
  return "Unable to contact the refresh service. The job may still be running. Check status before starting another refresh.";
}
export function Refresh() {
  const { api, user } = useSession();
  const cache = useQueryClient();
  const [jobId, setJobId] = useState("");
  const [start, setStart] = useState(""); const [end, setEnd] = useState(""); const [overlap, setOverlap] = useState(7);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const submitting = useRef(false);
  const [recovering, setRecovering] = useState(false);
  const recoveringNow = useRef(false);
  const [admissionUnknown, setAdmissionUnknown] = useState(false);
  useEffect(() => () => { controller.current?.abort(); }, []);
  const job = useQuery({ queryKey: ["refresh", user?.username, jobId],
    queryFn: ({ signal }) => api.request<RefreshJob>(`/refresh/${encodeURIComponent(jobId)}`, { signal }),
    enabled: user?.role === "admin" && Boolean(jobId), retry: false, gcTime: 0,
    refetchOnWindowFocus: false,
    refetchInterval: query => !query.state.error && active(query.state.data) ? 2000 : false,
  });
  useEffect(() => {
    if (job.data?.status === "succeeded" && job.data.published === true && job.data.outcome === "published") setStart("");
  }, [job.data]);
  function track(response: RefreshJob) {
    cache.setQueryData(["refresh", user?.username, response.job_id], response);
    setJobId(response.job_id); setAdmissionUnknown(false); setError("");
  }
  async function recover() {
    if (recoveringNow.current) return;
    recoveringNow.current = true; setRecovering(true);
    const attempt = new AbortController(); controller.current = attempt;
    try {
      const response = await api.request<RefreshJob | null>("/refresh/active", { signal: attempt.signal });
      if (attempt.signal.aborted) return;
      if (response) track(response);
      else { setAdmissionUnknown(false); setError("No active job is known to this backend process. Any earlier publication outcome remains unknown; check the ingestion audit."); }
    } catch (failure) { if (!attempt.signal.aborted) setError(refreshError(failure)); }
    finally { recoveringNow.current = false; if (!attempt.signal.aborted) setRecovering(false); }
  }
  const mutation = useMutation({ mutationFn: async () => {
    const attempt = new AbortController(); controller.current = attempt; setError("");
    try {
      const response = await api.request<RefreshJob>("/refresh", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ end_date: end, ...(start ? { start_date: start } : {}), overlap_days: overlap, conflict_policy: "reject" }), signal: attempt.signal });
      if (!attempt.signal.aborted) {
        track(response);
      }
    } catch (failure) {
      if (!attempt.signal.aborted) {
        setError(refreshError(failure));
        if (!(failure instanceof ApiError) || (failure.status === 0 || failure.status === 409 || failure.status >= 500)) setAdmissionUnknown(true);
      }
    }
    finally { submitting.current = false; }
  }, retry: false });
  if (user?.role !== "admin") return null;
  function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current || recoveringNow.current || admissionUnknown || active(job.data) || Boolean(jobId && !job.data)) return;
    submitting.current = true; mutation.mutate();
  }
  const unknownJob = job.isError && job.error instanceof ApiError && job.error.status === 404;
  const busy = mutation.isPending || recovering || admissionUnknown || active(job.data) || Boolean(jobId && !job.data);
  return <section className="mt-5 space-y-5" aria-label="Admin refresh">
    <p className="text-sm text-slate-600">Refresh all three routes atomically. Start date is required for the first publication; later runs use published watermarks. Conflicting corrections are rejected. Leaving this section does not cancel an accepted job.</p>
    <p id="first-publication-help" className="text-sm text-slate-600">Required only when creating the first production snapshot. If data was already published through the application or CLI, leave this blank; subsequent refreshes derive their starting point from published watermarks.</p>
    <form onSubmit={submit} className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-3">
        <label className="text-sm">First publication start date (optional)<input aria-describedby="first-publication-help" type="date" value={start} onChange={e => setStart(e.target.value)} className={`${inputStyle} mt-2`} disabled={busy} /></label>
        <label className="text-sm">Refresh through<input type="date" required min={start || undefined} value={end} onChange={e => setEnd(e.target.value)} className={`${inputStyle} mt-2`} disabled={busy} /></label>
        <label className="text-sm">Overlap days<input type="number" min={0} max={3660} required value={overlap} onChange={e => setOverlap(Number(e.target.value))} className={`${inputStyle} mt-2`} disabled={busy} /></label>
      </div>
      <Button disabled={busy} type="submit">{mutation.isPending ? "Starting refresh…" : unknownJob ? "Refresh outcome unknown" : active(job.data) ? "Refresh in progress…" : "Start refresh"}</Button>
    </form>
    {error && <p role="alert">{error}</p>}
    {(!jobId || admissionUnknown) && <Button disabled={mutation.isPending || recovering} onClick={() => void recover()}>{recovering ? "Checking active refresh…" : "Find active refresh"}</Button>}
    {jobId && <div className="space-y-3 rounded-md border border-slate-200 p-4">
      <p className="break-all text-sm">Job ID: {jobId}</p>
      {job.isError && <><p role="alert">{refreshError(job.error)}</p><Button onClick={() => void job.refetch()}>Retry status</Button></>}
      {unknownJob && <Button onClick={() => {
        cache.removeQueries({ queryKey: ["refresh", user?.username, jobId], exact: true });
        setJobId(""); setAdmissionUnknown(false); setError("Unavailable tracking cleared. Publication outcome remains unknown; backend admission and locking still apply.");
      }}>Clear unavailable job tracking</Button>}
      {job.data && !unknownJob && <>
        <p role="status" className="font-medium capitalize">{job.data.status}</p>
        <dl className="grid gap-2 text-sm sm:grid-cols-2">{[["Created", job.data.created_at], ["Started", job.data.started_at], ["Finished", job.data.finished_at]].map(([label, value]) => <div key={label}><dt className="font-medium">{label}</dt><dd>{value ? <time dateTime={value}>{value}</time> : "Not yet"}</dd></div>)}</dl>
        <p className="text-sm">Publication: {job.data.outcome === "no_op" ? "No changes needed" : job.data.published === true ? "Published" : job.data.published === false ? "Not published" : "Not yet confirmed"}</p>
        {job.data.error && <p role="alert">{job.data.error}</p>}
        {job.data.warning && <p role="alert">{job.data.warning}</p>}
        <dl className="text-sm">{Object.entries(job.data.row_counts).map(([route, count]) => <div key={route}><dt className="inline font-medium">{route}: </dt><dd className="inline">{count} snapshot rows</dd></div>)}
          {Object.entries(job.data.fetched_rows).map(([route, count]) => <div key={route}><dt className="inline font-medium">{route}: </dt><dd className="inline">{count} fetched rows</dd></div>)}</dl>
      </>}
    </div>}
  </section>;
}
