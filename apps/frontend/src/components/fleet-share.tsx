import { useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSession } from "./session";
import { Button } from "./ui/button";
import { DataTable } from "./data-table";
import { dataError, inputStyle, type Column } from "@/lib/data";

interface FleetDay {
  outage_date: string; total_fleet_capacity_mw: string | null; offline_capacity_mw: string | null;
  fleet_offline_share_pct: string | null;
  status: "ok" | "missing_date" | "duplicate_key" | "missing_values" | "unsupported_values" | "inconsistent_values" | "zero_capacity";
}
interface FleetResult { rows: FleetDay[] }
const columns: Column[] = [
  { name: "Date", type: "DATE" }, { name: "Fleet capacity", type: "MW" }, { name: "Offline capacity", type: "MW" },
  { name: "Offline share", type: "% (6 decimal places)" }, { name: "Status", type: "Validation status" },
];
export function FleetShare() {
  const { api, user } = useSession();
  const [start, setStart] = useState(""); const [end, setEnd] = useState("");
  const [window, setWindow] = useState<{ start: string; end: string } | null>(null);
  const [error, setError] = useState("");
  const result = useQuery({ queryKey: ["fleet-share", user?.username, user?.role, window],
    queryFn: ({ signal }) => api.request<FleetResult>(`/metrics/fleet-offline-share?${new URLSearchParams({ start_date: window!.start, end_date: window!.end })}`, { signal }),
    enabled: Boolean(window), retry: false, gcTime: 0,
  });
  function edit(value: string, field: "start" | "end") {
    setWindow(null); setError(""); if (field === "start") setStart(value); else setEnd(value);
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!start || !end || start > end || (Date.parse(end) - Date.parse(start)) / 86400000 >= 366) {
      setWindow(null); setError("Choose an inclusive date window of at most 366 days, with start on or before end."); return;
    }
    if (window?.start === start && window.end === end) void result.refetch(); else setWindow({ start, end });
  }
  const data = window && !result.isFetching && !result.isError ? result.data : undefined;
  return <section className="mt-5 space-y-5" aria-label="Daily fleet offline share">
    <p className="text-sm leading-6 text-slate-600">US-08: national outage MW ÷ national fleet capacity MW × 100. One result per requested day, rounded to six decimal places. Undefined or invalid values remain unavailable rather than becoming zero.</p>
    <form onSubmit={submit} className="flex flex-wrap items-end gap-4">
      <label className="text-sm">Metric start date<input type="date" required value={start} onChange={e => edit(e.target.value, "start")} className={`${inputStyle} mt-2`} /></label>
      <label className="text-sm">Metric end date<input type="date" required value={end} onChange={e => edit(e.target.value, "end")} className={`${inputStyle} mt-2`} /></label>
      <Button type="submit" disabled={Boolean(window && result.isFetching)}>Load daily metric</Button>
    </form>
    {error && <p role="alert">{error}</p>}
    {window && result.isFetching && <p role="status">Loading daily metric…</p>}
    {window && result.isError && <p role="alert">{dataError(result.error)}</p>}
    {data && <>
      <p className="text-sm">{data.rows.length} daily results. {data.rows.filter(row => row.status !== "ok").length} days have unavailable percentages; see the status column.</p>
      {data.rows.some(row => row.status === "ok") ? <figure aria-label="Daily national fleet offline percentage trend" className="max-h-80 overflow-y-auto rounded-md border border-slate-200 p-4">
        <figcaption className="mb-3 text-sm font-medium">Daily fleet offline share · fixed 0–100% scale</figcaption>
        <ol className="space-y-2">{data.rows.map(row => <li key={row.outage_date} className="grid grid-cols-[6rem_1fr_6rem] items-center gap-2 text-xs">
          <time dateTime={row.outage_date}>{row.outage_date}</time>
          <div aria-hidden="true" className="h-3 rounded bg-slate-100">{row.status === "ok" && row.fleet_offline_share_pct !== null && <div className="h-3 rounded bg-blue-600" style={{ width: `${Number(row.fleet_offline_share_pct)}%` }} />}</div>
          <span>{row.fleet_offline_share_pct === null ? "Unavailable" : `${row.fleet_offline_share_pct}%`}</span>
        </li>)}</ol>
      </figure> : <p role="status">No valid percentages are available for this window.</p>}
      <DataTable caption="Daily national fleet offline share" columns={columns} rows={data.rows.map(row => [row.outage_date, row.total_fleet_capacity_mw, row.offline_capacity_mw, row.fleet_offline_share_pct, row.status])} />
    </>}
  </section>;
}
