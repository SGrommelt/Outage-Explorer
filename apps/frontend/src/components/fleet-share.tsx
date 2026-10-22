import { useEffect, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { formatDecimal, formatPercent } from "@/lib/format";
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
  const [window, setWindow] = useState<{ start: string; end: string } | null>({ start: "", end: "" });
  const [error, setError] = useState("");
  const result = useQuery({ queryKey: ["fleet-share", user?.username, user?.role, window],
    queryFn: ({ signal }) => api.request<FleetResult>(`/metrics/fleet-offline-share${window?.start ? `?${new URLSearchParams({ start_date: window.start, end_date: window.end })}` : ""}`, { signal }),
    enabled: Boolean(window), retry: false, gcTime: 0,
  });
  useEffect(() => {
    if (window && !window.start && result.data?.rows.length) {
      setStart(result.data.rows[0].outage_date);
      setEnd(result.data.rows.at(-1)!.outage_date);
    }
  }, [window, result.data]);
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
  const latest = data?.rows.filter(row => row.status !== "missing_date").at(-1);
  return <section className="mt-5 space-y-5" aria-label="Daily fleet offline share">
    <p className="text-sm leading-6 text-slate-600">US-08: national outage MW ÷ national fleet capacity MW × 100. One result per requested day, rounded to six decimal places. Undefined or invalid values remain unavailable rather than becoming zero.</p>
    <form onSubmit={submit} className="flex flex-wrap items-end gap-4">
      <label className="text-sm">Metric start date<input type="date" required value={start} onChange={e => edit(e.target.value, "start")} className={`${inputStyle} mt-2`} /></label>
      <label className="text-sm">Metric end date<input type="date" required value={end} onChange={e => edit(e.target.value, "end")} className={`${inputStyle} mt-2`} /></label>
      <Button type="submit" disabled={Boolean(window && result.isFetching)}>Load daily metric</Button>
    </form>
    <p className="text-xs text-slate-500">Defaults to the latest 30 calendar days ending at the latest national observation, clipped to available history. Missing days remain unavailable. Dates use YYYY-MM-DD.</p>
    {error && <p role="alert">{error}</p>}
    {window && result.isFetching && <p role="status">Loading daily metric…</p>}
    {window && result.isError && <div><p role="alert">{dataError(result.error)}</p><Button onClick={() => void result.refetch()}>Retry daily metric</Button></div>}
    {data && <>
      <p className="text-sm">{data.rows.length} daily results. {data.rows.filter(row => row.status !== "ok").length} days have unavailable percentages; see the status column.</p>
      {latest && <div className="rounded-lg border border-blue-100 bg-blue-50 p-5 sm:p-6" aria-label="Latest observed fleet offline share">
        <p className="text-sm font-medium text-blue-900">Latest observation in selected period · <time dateTime={latest.outage_date}>{latest.outage_date}</time></p>
        <p className="my-2 text-4xl font-semibold tracking-tight text-slate-900 sm:text-5xl">{formatPercent(latest.status === "ok" ? latest.fleet_offline_share_pct : null)}</p>
        <p className="text-sm text-slate-700">National outage MW / national capacity MW × 100</p>
        <p className="mt-2 text-sm text-slate-600">{latest.status === "ok" ? `${formatDecimal(latest.offline_capacity_mw)} MW offline of ${formatDecimal(latest.total_fleet_capacity_mw)} MW capacity` : `Percentage undefined or invalid (${latest.status}).`} · Historical observation, not live telemetry.</p>
      </div>}
      {data.rows.some(row => row.status === "ok" && row.fleet_offline_share_pct !== null)
        ? <FleetTrend rows={data.rows} /> : <p role="status">No valid percentages are available for this window.</p>}
      <DataTable caption="Daily national fleet offline share" columns={columns} rows={data.rows.map(row => [row.outage_date, row.total_fleet_capacity_mw, row.offline_capacity_mw, row.status === "ok" ? row.fleet_offline_share_pct : null, row.status])} />
    </>}
  </section>;
}

/** Number conversion is confined to SVG coordinates; table and labels retain exact API strings. */
function FleetTrend({ rows }: { rows: FleetDay[] }) {
  const point = (row: FleetDay, index: number) => ({
    x: 48 + (rows.length === 1 ? 0.5 : index / (rows.length - 1)) * 600,
    y: 185 - Number(row.fleet_offline_share_pct) * 1.6,
  });
  const valid = (row: FleetDay) => row.status === "ok" && row.fleet_offline_share_pct !== null;
  return <figure aria-label="Daily national fleet offline percentage trend" className="rounded-md border border-slate-200 p-3 sm:p-4">
    <figcaption className="text-sm font-medium">Daily fleet offline share · fixed 0–100% scale</figcaption>
    <svg viewBox="0 0 680 225" role="img" aria-labelledby="fleet-trend-title" aria-describedby="fleet-trend-description" className="mt-2 h-auto w-full">
      <title id="fleet-trend-title">Historical daily national fleet offline share</title>
      <desc id="fleet-trend-description">Percent offline on a fixed 0 to 100 percent scale. Gaps indicate missing or invalid percentages and are not connected. Exact dates, values, and validation statuses are in the table below.</desc>
      {[0, 50, 100].map(tick => <g key={tick}><line x1="48" x2="648" y1={185 - tick * 1.6} y2={185 - tick * 1.6} stroke="#e2e8f0" /><text x="40" y={189 - tick * 1.6} textAnchor="end" fontSize="11" fill="#475569">{tick}%</text></g>)}
      {rows.map((row, index) => {
        if (!valid(row)) return null;
        const current = point(row, index); const previous = rows[index - 1];
        const consecutive = previous && Date.parse(row.outage_date) - Date.parse(previous.outage_date) === 86400000;
        return <g key={row.outage_date}>
          {consecutive && valid(previous) && <line data-testid="trend-segment" x1={point(previous, index - 1).x} y1={point(previous, index - 1).y} x2={current.x} y2={current.y} stroke="#2563eb" strokeWidth="2" />}
          <circle data-testid="trend-point" cx={current.x} cy={current.y} r="3" fill="#2563eb"><title>{row.outage_date}: {formatPercent(row.fleet_offline_share_pct)}</title></circle>
        </g>;
      })}
      <text x="48" y="211" fontSize="11" fill="#475569">{rows[0]?.outage_date}</text>
      <text x="648" y="211" textAnchor="end" fontSize="11" fill="#475569">{rows.at(-1)?.outage_date}</text>
    </svg>
    <p className="text-xs text-slate-500">Missing and invalid days break the line. An observed 0% remains a plotted value. Full-precision percentages appear in the table.</p>
  </figure>;
}
