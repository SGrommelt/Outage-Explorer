import { useEffect, useRef, useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { useSession } from "./session";
import { useCatalog } from "./datasets";
import { DataTable } from "./data-table";
import { Button } from "./ui/button";
import { dataError, inputStyle, type QueryResult } from "@/lib/data";

export function SqlExplorer() {
  const { api, user } = useSession();
  const catalog = useCatalog();
  const [sql, setSql] = useState("");
  const controller = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const [result, setResult] = useState<QueryResult | null>(null);
  const [error, setError] = useState("");
  const mutation = useMutation({ mutationFn: async ({ text, version }: { text: string; version: number }) => {
    controller.current?.abort(); const attempt = new AbortController(); controller.current = attempt;
    try {
      const response = await api.request<QueryResult>("/query", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sql: text }), signal: attempt.signal });
      if (!attempt.signal.aborted && generation.current === version) setResult(response);
    } catch (failure) { if (!attempt.signal.aborted && generation.current === version) setError(dataError(failure)); }
  }, gcTime: 0, onSettled: (): void => { mutation.reset(); } });
  useEffect(() => () => { generation.current++; controller.current?.abort(); }, []);
  if (!user || user.role === "viewer") return null;
  const example = catalog.data?.datasets.find(dataset => /^[a-z_]+$/.test(dataset.name))?.name;
  function edit(value: string) { generation.current++; controller.current?.abort(); setSql(value); setResult(null); setError(""); }
  function submit(event: FormEvent) { event.preventDefault(); setResult(null); setError(""); mutation.mutate({ text: sql, version: ++generation.current }); }
  return <section className="mt-5 space-y-4" aria-label="SQL query">
    <p className="text-sm leading-6 text-slate-600">One SELECT from one authorized dataset. Select * or named columns; optional WHERE, ORDER BY, and LIMIT. Basic comparisons, AND/OR/NOT, LIKE, IN, BETWEEN, IS NULL, and DATE literals are supported. No joins, subqueries, functions, aliases, aggregation, or writes. The server returns at most 200 rows.</p>
    {catalog.isError && <p role="alert">{dataError(catalog.error)}</p>}
    <form onSubmit={submit} className="space-y-3">
      <label className="block text-sm font-medium" htmlFor="sql">SQL statement</label>
      <textarea id="sql" value={sql} onChange={e => edit(e.target.value)} rows={7} maxLength={10000} required spellCheck={false} className={`${inputStyle} font-mono`} />
      <div className="flex flex-wrap gap-3">{example && <Button type="button" onClick={() => edit(`SELECT * FROM ${example} ORDER BY outage_date DESC LIMIT 50`)}>Use example query</Button>}
        <Button type="submit" disabled={!sql.trim() || mutation.isPending}>{mutation.isPending ? "Running query…" : "Run query"}</Button></div>
    </form>
    {mutation.isPending && <p role="status">Executing read-only query…</p>}
    {error && <p role="alert">{error}</p>}
    {result && <div><p role="status">{result.row_count} rows returned · {result.truncated ? `Truncated at the ${result.row_limit}-row server limit.` : "Not truncated."}</p><DataTable caption="SQL results" columns={result.columns} rows={result.rows} /></div>}
  </section>;
}
