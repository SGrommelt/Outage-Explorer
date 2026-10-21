import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSession } from "./session";
import { Button } from "./ui/button";
import { DataTable } from "./data-table";
import { dataError, inputStyle, type Catalog, type Dataset, type Preview } from "@/lib/data";

export function useCatalog() {
  const { api, user } = useSession();
  return useQuery({ queryKey: ["catalog", user?.username, user?.role],
    queryFn: ({ signal }) => api.request<Catalog>("/datasets", { signal }), retry: false });
}
export function Datasets() {
  const catalog = useCatalog();
  const [selection, setSelection] = useState("");
  if (catalog.isPending) return <p role="status" className="mt-5">Loading datasets…</p>;
  if (catalog.isError) return <div className="mt-5"><p role="alert">{dataError(catalog.error)}</p><Button onClick={() => void catalog.refetch()}>Retry catalog</Button></div>;
  const datasets = catalog.data.datasets;
  const selected = datasets.find(dataset => dataset.name === selection) ?? datasets[0];
  if (!selected) return <p className="mt-5">No datasets are available for your account.</p>;
  return <div className="mt-5 space-y-5">
    <label className="block text-sm font-medium">Dataset<select value={selected.name} onChange={e => setSelection(e.target.value)} className={`${inputStyle} mt-2`}>
      {datasets.map(dataset => <option key={dataset.name} value={dataset.name}>{dataset.name}</option>)}
    </select></label>
    <p className="text-sm text-slate-600">{selected.description}</p>
    <details><summary className="cursor-pointer text-sm font-medium">Columns and data types</summary><dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2">{selected.columns.map(column => <div key={column.name}><dt className="font-medium">{column.name}</dt><dd className="text-slate-600">{column.type}</dd></div>)}</dl></details>
    <DatasetPreview key={selected.name} dataset={selected} />
  </div>;
}
function DatasetPreview({ dataset }: { dataset: Dataset }) {
  const { api, user } = useSession();
  const [start, setStart] = useState(""); const [end, setEnd] = useState(""); const [facility, setFacility] = useState("");
  const [page, setPage] = useState(1); const [pageSize, setPageSize] = useState(50);
  const facilitySupported = dataset.columns.some(column => column.name === "facility_id");
  const invalid = Boolean(start && end && start > end);
  const preview = useQuery({
    queryKey: ["preview", user?.username, user?.role, dataset.name, start, end, facility, page, pageSize],
    queryFn: ({ signal }) => {
      const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
      if (start) params.set("start_date", start); if (end) params.set("end_date", end);
      if (facilitySupported && facility.trim()) params.set("facility_id", facility.trim());
      return api.request<Preview>(`/datasets/${encodeURIComponent(dataset.name)}/preview?${params}`, { signal });
    }, enabled: !invalid, retry: false, gcTime: 0, staleTime: 0,
  });
  // Only a fresh response for the current parameters is rendered, never a previous page.
  const data = !invalid && !preview.isFetching && !preview.isError ? preview.data : undefined;
  return <section aria-label="Dataset preview">
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <label className="text-sm">Start date<input type="date" value={start} onChange={e => { setStart(e.target.value); setPage(1); }} className={`${inputStyle} mt-2`} /></label>
      <label className="text-sm">End date<input type="date" value={end} onChange={e => { setEnd(e.target.value); setPage(1); }} className={`${inputStyle} mt-2`} /></label>
      {facilitySupported && <label className="text-sm">Facility ID<input value={facility} onChange={e => { setFacility(e.target.value); setPage(1); }} className={`${inputStyle} mt-2`} placeholder="Exact identifier" /></label>}
      <label className="text-sm">Rows per page<select value={pageSize} onChange={e => { setPageSize(Number(e.target.value)); setPage(1); }} className={`${inputStyle} mt-2`}>{[25, 50, 100, 200].map(size => <option key={size}>{size}</option>)}</select></label>
    </div>
    {invalid ? <p role="alert" className="mt-4">Start date must be on or before end date.</p>
      : preview.isError ? <div className="mt-4"><p role="alert">{dataError(preview.error)}</p><Button onClick={() => void preview.refetch()}>Retry preview</Button></div>
      : !data ? <p role="status" className="mt-4">Loading rows…</p> : <>
        <p className="mt-5 text-sm" aria-live="polite">{data.total} matching rows · Page {data.page} of {Math.max(1, Math.ceil(data.total / data.page_size))}</p>
        <DataTable caption={`${dataset.name} preview`} columns={dataset.columns} rows={data.rows.map(row => dataset.columns.map(column => row[column.name]))} />
        <div className="mt-4 flex gap-3"><Button disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</Button><Button disabled={page * pageSize >= data.total} onClick={() => setPage(page + 1)}>Next</Button></div>
      </>}
  </section>;
}
