import { formatDecimal as formatMw } from "@/lib/format";
import { useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { dataError, inputStyle, type Cell, type Column } from "@/lib/data";
import { DataTable } from "./data-table";
import { useSession } from "./session";
import { Button } from "./ui/button";

type Status = "equal" | "difference" | "missing_level" | "invalid_data";
interface Reason { code: string; description: string }
interface Comparison { outage_date: string; status: Status; numeric_aggregates_match: boolean | null; reasons: Reason[] }
interface Daily extends Comparison {
  national_capacity_mw: string | null; facility_capacity_mw: string | null; generator_capacity_mw: string | null;
  national_outage_mw: string | null; facility_outage_mw: string | null; generator_outage_mw: string | null;
  facility_minus_national_mw: string | null; generator_minus_national_mw: string | null; generator_minus_facility_mw: string | null;
  facility_capacity_minus_national_mw: string | null; generator_capacity_minus_national_mw: string | null; generator_capacity_minus_facility_mw: string | null;
  national_count: number; facility_count: number; generator_count: number;
  missing_facility_keys: number | null; missing_generator_groups: number | null; facility_groups_with_differences: number | null;
}
interface Completeness { mode: "demo" | "production"; independently_verified: boolean; caveat: string }
interface DailyResponse {
  start_date: string | null; end_date: string | null; available_start: string | null; available_end: string | null;
  source_completeness: Completeness;
  summary: { days_requested: number; days_compared: number; days_matching: number; days_with_differences: number; days_unavailable: number };
  rows: Daily[];
}
interface Facility extends Comparison {
  facility_id: string | null; facility_capacity_mw: string | null; generator_capacity_mw: string | null;
  facility_outage_mw: string | null; generator_outage_mw: string | null; generator_count: number;
  generator_minus_facility_mw: string | null; generator_capacity_minus_facility_mw: string | null;
}
interface FacilityResponse { date: string; page: number; page_size: number; total: number; source_completeness: Completeness; rows: Facility[] }
const statusLabel: Record<Status, string> = { equal: "Matching aggregates", difference: "Differences", missing_level: "Missing coverage", invalid_data: "Unavailable values" };
export { formatDecimal as formatMw } from "@/lib/format";
function errorMessage(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 403) return "Reconciliation is available to Analyst and Admin only.";
    if (error.status === 400 || error.status === 422) return "Invalid reconciliation dates or pagination. Use at most 31 consecutive days.";
    if (error.status === 503) return "Reconciliation data is unavailable. A valid published snapshot or prepared isolated demo is required.";
  }
  return dataError(error);
}
const numericColumns = [
  ["national_outage_mw", "National outage"], ["facility_outage_mw", "Facility outage sum"], ["generator_outage_mw", "Generator outage sum"],
  ["national_capacity_mw", "National capacity"], ["facility_capacity_mw", "Facility capacity sum"], ["generator_capacity_mw", "Generator capacity sum"],
  ["facility_minus_national_mw", "Outage: facility − national"], ["generator_minus_national_mw", "Outage: generator − national"], ["generator_minus_facility_mw", "Outage: generator − facility"],
  ["facility_capacity_minus_national_mw", "Capacity: facility − national"], ["generator_capacity_minus_national_mw", "Capacity: generator − national"], ["generator_capacity_minus_facility_mw", "Capacity: generator − facility"],
] as const;
function Explanations({ rows }: { rows: Comparison[] }) {
  const differences = rows.filter(row => row.status !== "equal");
  if (!differences.length) return null;
  return <div className="mt-4 space-y-3" aria-label="Comparison explanations">{differences.map((row, index) => <div key={index} className="rounded-md bg-slate-50 p-3 text-sm">
    <p className="font-medium">{row.outage_date}{"facility_id" in row ? ` · Facility ${String(row.facility_id ?? "unknown")}` : ""}: {statusLabel[row.status]}</p>
    <ul className="mt-2 list-disc space-y-1 pl-5">{row.reasons.map(reason => <li key={reason.code}><span className="font-medium">{reason.code}: </span>{reason.description}</li>)}</ul>
  </div>)}</div>;
}
export function Reconciliation() {
  const { user } = useSession();
  if (!user || user.role === "viewer") return <p role="alert">Reconciliation is available to Analyst and Admin only.</p>;
  return <ReconciliationContent />;
}
function ReconciliationContent() {
  const { api, user } = useSession();
  const [start, setStart] = useState(""); const [end, setEnd] = useState("");
  const [parameters, setParameters] = useState<{ start: string; end: string } | null>({ start: "", end: "" });
  const [error, setError] = useState("");
  const response = useQuery({ queryKey: ["reconciliation", user?.username, user?.role, parameters],
    queryFn: ({ signal }) => {
      const params = new URLSearchParams();
      if (parameters?.start) params.set("start_date", parameters.start);
      if (parameters?.end) params.set("end_date", parameters.end);
      return api.request<DailyResponse>(`/reconciliation${params.size ? `?${params}` : ""}`, { signal });
    }, enabled: parameters !== null, retry: false, gcTime: 0,
  });
  function edit(value: string, field: "start" | "end") {
    setParameters(null); setError(""); if (field === "start") setStart(value); else setEnd(value);
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (start && end && (start > end || (Date.parse(end) - Date.parse(start)) / 86400000 > 30)) {
      setError("Choose a valid inclusive range of at most 31 days."); return;
    }
    setError("");
    if (parameters?.start === start && parameters.end === end) void response.refetch(); else setParameters({ start, end });
  }
  const data = parameters && !response.isFetching && !response.isError ? response.data : undefined;
  return <section aria-label="Reconciliation workspace" className="mt-5 space-y-5">
    <p className="text-sm leading-6 text-slate-600">Compare independent source grains by day. Matching MW aggregates show internal consistency and do not certify complete EIA source coverage. Signed gaps use the direction stated in each column. Zero daily gaps can still hide missing keys or offsetting facility differences.</p>
    <form onSubmit={submit} className="flex flex-wrap items-end gap-4">
      <label className="text-sm">Comparison start date<input type="date" value={start} onChange={e => edit(e.target.value, "start")} className={`${inputStyle} mt-2`} /></label>
      <label className="text-sm">Comparison end date<input type="date" value={end} onChange={e => edit(e.target.value, "end")} className={`${inputStyle} mt-2`} /></label>
      <Button type="submit" disabled={Boolean(parameters && response.isFetching)}>Compare period</Button>
      <p className="text-xs text-slate-500">Leave dates blank for the latest 30 available days. Maximum 31 days.</p>
    </form>
    {error && <p role="alert">{error}</p>}
    {parameters === null && !error && <p role="status">Apply the date range to compare.</p>}
    {parameters && response.isFetching && <p role="status">Loading reconciliation…</p>}
    {parameters && response.isError && <div><p role="alert">{errorMessage(response.error)}</p><Button onClick={() => void response.refetch()}>Retry reconciliation</Button></div>}
    {data && <PeriodResults key={`${data.start_date}/${data.end_date}`} data={data} />}
  </section>;
}
function PeriodResults({ data }: { data: DailyResponse }) {
  const [selected, setSelected] = useState(data.rows.at(-1)?.outage_date ?? "");
  const columns: Column[] = [{ name: "Date", type: "DATE" }, ...numericColumns.map(([, label]) => ({ name: label, type: "MW" })),
    ...["National rows", "Facility rows", "Generator rows", "Missing facility keys", "Missing generator groups", "Facility groups with MW gaps"].map(name => ({ name, type: "Count" })),
    { name: "Comparison", type: "Status" }];
  const rows: Cell[][] = data.rows.map(row => [row.outage_date, ...numericColumns.map(([field]) => formatMw(row[field])),
    row.national_count, row.facility_count, row.generator_count, row.missing_facility_keys, row.missing_generator_groups, row.facility_groups_with_differences, statusLabel[row.status]]);
  return <div className="space-y-5">
    <p role="note" aria-label="Source completeness" className={`rounded-md border p-4 text-sm ${data.source_completeness.mode === "demo" ? "border-amber-200 bg-amber-50 text-amber-950" : "border-slate-200 bg-slate-50"}`}>{data.source_completeness.caveat}</p>
    <p className="text-sm">Selected period: {data.start_date ?? "unavailable"} – {data.end_date ?? "unavailable"}. Available observations: {data.available_start ?? "none"} – {data.available_end ?? "none"}.</p>
    <dl aria-label="Period summary" className="grid grid-cols-2 gap-3 lg:grid-cols-4">{[
      ["Days compared", data.summary.days_compared], ["Days matching", data.summary.days_matching], ["Days with differences", data.summary.days_with_differences], ["Days unavailable", data.summary.days_unavailable],
    ].map(([label, count]) => <div key={label} className="rounded-md border border-slate-200 p-3"><dt className="text-xs text-slate-500">{label}</dt><dd className="mt-1 text-2xl font-semibold">{count}</dd></div>)}</dl>
    {!data.rows.some(row => row.national_count || row.facility_count || row.generator_count) && <p role="status">No source observations are available for this period.</p>}
    <p className="text-xs text-slate-500">MW values retain up to six decimal places; trailing zeros are omitted. Scroll horizontally to compare all grains and gaps.</p>
    {data.rows.length > 0 && <><DataTable columns={columns} rows={rows} caption="Daily reconciliation" /><Explanations rows={data.rows} />
      <section aria-label="Facility drill-down" className="space-y-3 border-t border-slate-200 pt-5">
        <h2 className="text-lg font-semibold">Facility and generator comparison</h2>
        <label className="block max-w-xs text-sm">Detail date<select value={selected} onChange={e => setSelected(e.target.value)} className={`${inputStyle} mt-2`}>{data.rows.map(row => <option key={row.outage_date}>{row.outage_date}</option>)}</select></label>
        <FacilityDetails key={selected} date={selected} />
      </section></>}
  </div>;
}
function FacilityDetails({ date }: { date: string }) {
  const { api, user } = useSession(); const [page, setPage] = useState(1);
  const result = useQuery({ queryKey: ["reconciliation-facilities", user?.username, user?.role, date, page],
    queryFn: ({ signal }) => api.request<FacilityResponse>(`/reconciliation/facilities?${new URLSearchParams({ date, page: String(page), page_size: "25" })}`, { signal }), retry: false, gcTime: 0,
  });
  if (result.isError) return <div><p role="alert">{errorMessage(result.error)}</p><Button onClick={() => void result.refetch()}>Retry facility comparison</Button></div>;
  if (result.isFetching || !result.data) return <p role="status">Loading facility comparisons…</p>;
  const data = result.data;
  const columns: Column[] = [{ name: "Facility ID", type: "VARCHAR" }, ...["Facility outage", "Generator outage sum", "Outage: generator − facility", "Facility capacity", "Generator capacity sum", "Capacity: generator − facility"].map(name => ({ name, type: "MW" })), { name: "Generator rows", type: "Count" }, { name: "Comparison", type: "Status" }];
  const rows: Cell[][] = data.rows.map(row => [row.facility_id, ...[row.facility_outage_mw, row.generator_outage_mw, row.generator_minus_facility_mw, row.facility_capacity_mw, row.generator_capacity_mw, row.generator_capacity_minus_facility_mw].map(formatMw), row.generator_count, statusLabel[row.status]]);
  return <div><p className="text-sm">{data.total} facility groups · Page {data.page} of {Math.max(1, Math.ceil(data.total / data.page_size))}</p>
    <DataTable caption="Facility reconciliation" columns={columns} rows={rows} /><Explanations rows={data.rows} />
    <div className="mt-4 flex gap-3"><Button disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous facilities</Button><Button disabled={page * data.page_size >= data.total} onClick={() => setPage(page + 1)}>Next facilities</Button></div>
  </div>;
}
