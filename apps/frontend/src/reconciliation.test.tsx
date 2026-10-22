import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, vi } from "vitest";
import App from "./App";
import { formatMw } from "./components/reconciliation";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const reasons = [{ code: "aggregates_match", description: "Internal numeric consistency only." }];
const day = {
  outage_date: "2026-01-01", status: "equal", numeric_aggregates_match: true, reasons,
  national_capacity_mw: "100056.700000", facility_capacity_mw: "100056.700000", generator_capacity_mw: "100056.700000",
  national_outage_mw: "19859.767000", facility_outage_mw: "19859.767000", generator_outage_mw: "19859.767000",
  facility_minus_national_mw: "0.000000", generator_minus_national_mw: "0.000000", generator_minus_facility_mw: "0.000000",
  facility_capacity_minus_national_mw: "0.000000", generator_capacity_minus_national_mw: "0.000000", generator_capacity_minus_facility_mw: "0.000000",
  national_count: 1, facility_count: 55, generator_count: 95, missing_facility_keys: 0, missing_generator_groups: 0, facility_groups_with_differences: 0,
};
const completeness = { mode: "demo", independently_verified: false, caveat: "DEMO: source completeness is unverified. Facility totals are inconsistent; numeric matches do not resolve it." };
const report = { start_date: "2026-01-01", end_date: "2026-01-02", available_start: "2026-01-01", available_end: "2026-01-02",
  source_completeness: completeness, summary: { days_requested: 2, days_compared: 2, days_matching: 2, days_with_differences: 0, days_unavailable: 0 }, rows: [day, { ...day, outage_date: "2026-01-02" }] };
const group = { outage_date: "2026-01-02", facility_id: "F1", status: "equal", numeric_aggregates_match: true, reasons,
  facility_capacity_mw: "300.000000", generator_capacity_mw: "300.000000", facility_outage_mw: "30.000000", generator_outage_mw: "30.000000", generator_count: 2, generator_minus_facility_mw: "0.000000", generator_capacity_minus_facility_mw: "0.000000" };
let role = "analyst";
let calls: { url: string; options: RequestInit }[];
let handle: (url: URL, options: RequestInit) => Response | Promise<Response>;
function setup() {
  role = "analyst"; calls = [];
  handle = url => url.pathname.endsWith("/facilities") ? json({ date: url.searchParams.get("date"), page: Number(url.searchParams.get("page")), page_size: 25, total: 55, source_completeness: completeness,
    rows: [{ ...group, outage_date: url.searchParams.get("date"), facility_id: `F${url.searchParams.get("page")}` }] }) : json(report);
  vi.stubGlobal("fetch", vi.fn(async (url: string, options: RequestInit) => {
    calls.push({ url, options });
    if (url === "/api/auth/login") return json({ access_token: `token-${role}`, token_type: "bearer", expires_in: 600 });
    if (url === "/api/auth/me") return json({ username: role, role });
    if (url === "/api/datasets") return json({ datasets: [] });
    return handle(new URL(url, "http://localhost"), options);
  }));
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><App /></QueryClientProvider>);
}
async function login() {
  fireEvent.change(screen.getByLabelText("Username"), { target: { value: role } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "synthetic-password" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  await screen.findByRole("navigation");
}
async function enter() { await login(); fireEvent.click(screen.getByRole("button", { name: "Reconciliation" })); }
afterEach(() => vi.unstubAllGlobals());

test.each(["viewer", "analyst", "admin"])("navigation honors %s reconciliation permissions", async persona => {
  setup(); role = persona; await login();
  expect(screen.queryByRole("button", { name: "Reconciliation" }) !== null).toBe(persona !== "viewer");
  expect(calls.some(call => call.url.startsWith("/api/reconciliation"))).toBe(false);
});

test("daily grains, exact MW formatting, summary, and authoritative demo caveat render", async () => {
  setup(); await enter();
  const table = await screen.findByRole("table", { name: "Daily reconciliation" });
  expect(within(table).getAllByRole("cell", { name: "100,056.7" })).toHaveLength(6);
  expect(within(table).getAllByRole("cell", { name: "19,859.767" })).toHaveLength(6);
  expect(screen.getByRole("note", { name: "Source completeness" })).toHaveTextContent("source completeness is unverified");
  expect(screen.getByLabelText("Period summary")).toHaveTextContent("Days matching2");
  expect(within(table).getByRole("columnheader", { name: /Capacity: generator − facility/ })).toBeVisible();
  expect(await screen.findByRole("cell", { name: "F1" })).toBeVisible();
});

test("date selection and facility pagination use bounded backend requests", async () => {
  setup(); await enter(); await screen.findByRole("cell", { name: "F1" });
  fireEvent.click(screen.getByRole("button", { name: "Next facilities" }));
  await screen.findByRole("cell", { name: "F2" });
  expect(calls.at(-1)?.url).toContain("page=2&page_size=25");
  fireEvent.change(screen.getByLabelText("Detail date"), { target: { value: "2026-01-01" } });
  await screen.findByRole("cell", { name: "F1" });
  expect(calls.at(-1)?.url).toContain("date=2026-01-01&page=1");
  expect(screen.getByRole("button", { name: "Previous facilities" })).toBeDisabled();
});

test("editing filters clears data; invalid range stays local; valid range is sent", async () => {
  setup(); await enter(); await screen.findByRole("table", { name: "Daily reconciliation" });
  fireEvent.change(screen.getByLabelText("Comparison start date"), { target: { value: "2026-02-01" } });
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Comparison end date"), { target: { value: "2026-01-01" } });
  fireEvent.click(screen.getByRole("button", { name: "Compare period" }));
  expect(screen.getByRole("alert")).toHaveTextContent("at most 31 days");
  expect(calls.filter(call => call.url === "/api/reconciliation")).toHaveLength(1);
  fireEvent.change(screen.getByLabelText("Comparison start date"), { target: { value: "2026-01-01" } });
  fireEvent.change(screen.getByLabelText("Comparison end date"), { target: { value: "2026-01-02" } });
  fireEvent.click(screen.getByRole("button", { name: "Compare period" }));
  await waitFor(() => expect(calls.some(call => call.url === "/api/reconciliation?start_date=2026-01-01&end_date=2026-01-02")).toBe(true));
});

test("loading states expose no stale rows", async () => {
  setup(); let resolve!: (value: Response) => void;
  handle = () => new Promise<Response>(r => { resolve = r; });
  await enter();
  expect(screen.getByRole("status")).toHaveTextContent("Loading reconciliation");
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  handle = () => json({ date: "2026-01-02", total: 0, page: 1, page_size: 25, source_completeness: completeness, rows: [] });
  await act(async () => resolve(json(report)));
  await screen.findByRole("table", { name: "Daily reconciliation" });
});

test.each([403, 503, 400, 0])("safe error state for reconciliation status %s", async status => {
  setup(); handle = () => { if (!status) throw new TypeError("private host"); return json({ detail: "private path" }, status); };
  await enter();
  const alert = await screen.findByRole("alert");
  expect(alert).not.toHaveTextContent("private");
  if (status === 403) expect(alert).toHaveTextContent("Analyst and Admin only");
  if (status === 503) expect(alert).toHaveTextContent("unavailable");
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});

test("empty data has a clear state and no invented values", async () => {
  setup(); handle = () => json({ ...report, start_date: null, end_date: null, available_start: null, available_end: null, summary: { days_requested: 0, days_compared: 0, days_matching: 0, days_with_differences: 0, days_unavailable: 0 }, rows: [] });
  await enter();
  expect(await screen.findByText("No source observations are available for this period.")).toBeVisible();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});

test("discrepancy descriptions and micro-MW gaps remain visible without causal claims", async () => {
  setup();
  const original = handle;
  handle = (url, options) => url.pathname.endsWith("/facilities") ? original(url, options) : json({ ...report, rows: [{ ...day,
    status: "difference", numeric_aggregates_match: false, generator_minus_facility_mw: "-0.000001",
    reasons: [{ code: "outage_mismatch", description: "Reported outages differ; exact gap retained." }, { code: "unknown_cause", description: "Source cause is unverified; no explanation is asserted." }] }] });
  await enter();
  expect(await screen.findByRole("cell", { name: "-0.000001" })).toBeVisible();
  expect(screen.getByText("Source cause is unverified; no explanation is asserted.")).toBeVisible();
});

test("missing coverage renders unavailable values and evidence-based explanation", async () => {
  setup(); const original = handle;
  handle = (url, options) => url.pathname.endsWith("/facilities") ? original(url, options) : json({ ...report, rows: [{ ...day, status: "missing_level", national_capacity_mw: null, national_outage_mw: null, national_count: 0,
    reasons: [{ code: "missing_national_coverage", description: "No national observation is available for this requested date." }] }] });
  await enter();
  expect(await screen.findByText("No national observation is available for this requested date.")).toBeVisible();
  expect(screen.getAllByRole("cell", { name: "Unavailable" })).toHaveLength(2);
});

test("logout aborts an outstanding request and a Viewer cannot see late results", async () => {
  setup(); let resolve!: (value: Response) => void; let signal: AbortSignal | null | undefined;
  handle = (_, options) => new Promise<Response>(r => { resolve = r; signal = options.signal; });
  await enter(); await waitFor(() => expect(signal).toBeDefined());
  fireEvent.click(screen.getByRole("button", { name: "Log out" }));
  expect(signal?.aborted).toBe(true);
  role = "viewer"; await login();
  await act(async () => resolve(json(report)));
  expect(screen.queryByRole("button", { name: "Reconciliation" })).not.toBeInTheDocument();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});

test("MW formatting keeps every significant digit without binary float rounding", () => {
  expect(formatMw("999999999999999999.123456")).toBe("999,999,999,999,999,999.123456");
  expect(formatMw("-0.000001")).toBe("-0.000001");
  expect(formatMw(null)).toBe("Unavailable");
});

test("facility request errors are safe and retryable", async () => {
  setup(); const original = handle;
  let fail = true;
  handle = (url, options) => url.pathname.endsWith("/facilities") && fail ? json({}, 503) : original(url, options);
  await enter();
  expect(await screen.findByRole("alert")).toHaveTextContent("unavailable");
  expect(screen.queryByRole("cell", { name: "F1" })).not.toBeInTheDocument();
  fail = false; fireEvent.click(screen.getByRole("button", { name: "Retry facility comparison" }));
  await screen.findByRole("cell", { name: "F1" });
});

test("an expired reconciliation session returns to login", async () => {
  setup(); handle = () => json({}, 401); await enter();
  expect(await screen.findByRole("alert")).toHaveTextContent("session has expired");
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});
