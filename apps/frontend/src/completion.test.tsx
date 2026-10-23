import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, vi } from "vitest";
import App from "./App";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const pending = { job_id: "local-job", status: "pending", created_at: "2026-01-03T00:00:00Z", started_at: null, finished_at: null, fetched_rows: {}, row_counts: {}, published: null, outcome: null, error: null, warning: null };
const day = { outage_date: "2026-01-01", total_fleet_capacity_mw: "300.000000", offline_capacity_mw: "100.000000", fleet_offline_share_pct: "33.333333", status: "ok" };
let role = "admin";
let handle: (url: string, options: RequestInit) => Promise<Response> | Response;
let calls: { url: string; options: RequestInit }[];
function setup() {
  role = "admin"; calls = [];
  handle = url => url === "/api/datasets" ? json({ datasets: [] }) : json({ rows: [day] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  vi.stubGlobal("fetch", vi.fn(async (url: string, options: RequestInit) => {
    calls.push({ url, options });
    if (url === "/api/auth/login") return json({ access_token: `token-${role}`, token_type: "bearer", expires_in: 600 });
    if (url === "/api/auth/me") return json({ username: role, role });
    return handle(url, options);
  }));
  return render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
}
async function login() {
  fireEvent.change(screen.getByLabelText("Username"), { target: { value: role } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "synthetic-test-password" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  await screen.findByRole("navigation");
}
function refreshSubmit() {
  fireEvent.change(screen.getByLabelText("Refresh through"), { target: { value: "2026-01-02" } });
  fireEvent.change(screen.getByLabelText("First publication start date (optional)"), { target: { value: "2026-01-01" } });
  fireEvent.click(screen.getByRole("button", { name: "Start refresh" }));
}
function metricSubmit() {
  fireEvent.change(screen.getByLabelText("Metric start date"), { target: { value: "2026-01-01" } });
  fireEvent.change(screen.getByLabelText("Metric end date"), { target: { value: "2026-01-03" } });
  fireEvent.click(screen.getByRole("button", { name: "Load daily metric" }));
}
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

test.each(["viewer", "analyst", "admin"])("%s sees national metric; only Admin sees refresh", async persona => {
  setup(); role = persona; await login();
  expect(screen.queryByRole("button", { name: "Refresh" }) !== null).toBe(role === "admin");
  fireEvent.click(screen.getByRole("button", { name: "Fleet offline share" })); metricSubmit();
  expect(await screen.findByRole("cell", { name: "33.333333" })).toBeVisible();
  expect(screen.getByRole("figure", { name: /Daily national/ })).toBeVisible();
  expect(screen.getByRole("cell", { name: "300" })).toBeVisible();
  expect(calls.filter(call => call.url.startsWith("/api/metrics/")).length).toBe(2);
  expect(calls.some(call => call.url.startsWith("/api/refresh"))).toBe(false);
});

test("metric invalid days remain null with readable statuses", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Fleet offline share" }));
  handle = () => json({ rows: ["missing_values", "zero_capacity", "duplicate_key", "missing_date", "inconsistent_values"].map((status, index) => ({ ...day, outage_date: `2026-01-0${index + 1}`, total_fleet_capacity_mw: null, offline_capacity_mw: null, fleet_offline_share_pct: null, status })) });
  metricSubmit();
  expect(await screen.findByText("No valid percentages are available for this window.")).toBeVisible();
  expect(screen.getByRole("cell", { name: "duplicate_key" })).toBeVisible();
  expect(screen.getAllByRole("cell", { name: "NULL" })).toHaveLength(15);
});

test("metric date edits clear old values and invalid ranges do not fetch", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Fleet offline share" })); metricSubmit();
  await screen.findByRole("cell", { name: "33.333333" });
  fireEvent.change(screen.getByLabelText("Metric start date"), { target: { value: "2027-01-01" } });
  expect(screen.queryByRole("cell")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Load daily metric" }));
  expect(screen.getByRole("alert")).toHaveTextContent("at most 366 days");
  expect(calls.filter(call => call.url.startsWith("/api/metrics/")).length).toBe(2);
});

test.each([503, 403, 0])("metric error %s shows a safe message", async status => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Fleet offline share" }));
  handle = () => { if (!status) throw new TypeError("private host"); return json({ detail: "private path" }, status); };
  metricSubmit(); expect(await screen.findByRole("alert")).not.toHaveTextContent("private");
  expect(screen.queryByRole("cell")).not.toBeInTheDocument();
});

test("refresh starts once, polls running job, renders success, and stops polling", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  let polls = 0;
  handle = (url, options) => {
    if (options.method === "POST") {
      expect(JSON.parse(options.body as string)).toEqual({ start_date: "2026-01-01", end_date: "2026-01-02", overlap_days: 7, conflict_policy: "reject" });
      return json(pending, 202);
    }
    polls++;
    return json({ ...pending, status: polls === 1 ? "running" : "succeeded", started_at: "2026-01-03T00:00:01Z", finished_at: polls === 1 ? null : "2026-01-03T00:00:04Z", row_counts: polls === 1 ? {} : { national_outages: 2 }, fetched_rows: { national_outages: 2 }, published: polls === 1 ? null : true, outcome: polls === 1 ? null : "published" });
  };
  refreshSubmit();
  expect(await screen.findByText("Job ID: local-job")).toBeVisible();
  expect(await screen.findByRole("status")).toHaveTextContent("running");
  fireEvent.click(screen.getByRole("button", { name: "Refresh in progress…" }));
  expect(calls.filter(call => call.options.method === "POST" && call.url === "/api/refresh")).toHaveLength(1);
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("succeeded"), { timeout: 4000 });
  expect(screen.getByText("2 snapshot rows")).toBeVisible();
  expect(screen.getByText("2026-01-03T00:00:04Z")).toBeVisible();
  const completedPolls = polls;
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 2200)); });
  expect(polls).toBe(completedPolls);
}, 10000);

test("failed refresh exposes sanitized server outcome without publishing", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  handle = (_, options) => json(options.method === "POST" ? pending : { ...pending, status: "failed", finished_at: "2026-01-03T00:01:00Z", published: false, error: "EIA credentials are not configured." });
  refreshSubmit();
  expect(await screen.findByRole("alert")).toHaveTextContent("EIA credentials are not configured.");
  expect(screen.getByRole("status")).toHaveTextContent("failed");
  expect(screen.getByText("Publication: Not published")).toBeVisible();
});

test.each([409, 0])("refresh admission failure %s is safe", async status => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  handle = () => { if (!status) throw new TypeError("private request URL"); return json({ detail: "private error" }, status); };
  refreshSubmit();
  expect(await screen.findByRole("alert")).toHaveTextContent(status === 409 ? "already pending or running" : "may still be running");
  expect(screen.getByRole("alert")).not.toHaveTextContent("private");
});

test("status network failure pauses polling and supports retry", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  let fail = true;
  handle = (_, options) => {
    if (options.method === "POST") return json(pending, 202);
    if (fail) throw new TypeError("private host");
    return json({ ...pending, status: "succeeded", finished_at: "2026-01-03T00:01:00Z", published: false, outcome: "no_op" });
  };
  refreshSubmit();
  await screen.findByRole("button", { name: "Retry status" });
  fail = false; fireEvent.click(screen.getByRole("button", { name: "Retry status" }));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("succeeded"));
  expect(screen.getByText("Publication: No changes needed")).toBeVisible();
});

test("logout aborts polling and hides job details from a new Viewer session", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  let signal: AbortSignal | null | undefined;
  let resolve!: (value: Response) => void;
  handle = (_, options) => options.method === "POST" ? json(pending, 202) : new Promise<Response>(r => { signal = options.signal; resolve = r; });
  refreshSubmit(); await screen.findByText("Job ID: local-job"); await waitFor(() => expect(signal).toBeDefined());
  fireEvent.click(screen.getByRole("button", { name: "Log out" }));
  expect(signal?.aborted).toBe(true);
  role = "viewer"; handle = url => json(url.startsWith("/api/metrics/") ? { rows: [] } : { datasets: [] }); await login();
  await act(async () => resolve(json({ ...pending, status: "succeeded" })));
  expect(screen.queryByText("Job ID: local-job")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Refresh" })).not.toBeInTheDocument();
});

test("401 during refresh polling clears session", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  handle = (_, options) => json(options.method === "POST" ? pending : {}, options.method === "POST" ? 202 : 401);
  refreshSubmit();
  expect(await screen.findByRole("alert")).toHaveTextContent("session has expired");
  expect(screen.queryByText("Job ID: local-job")).not.toBeInTheDocument();
});

test("metric values and late responses are cleared on logout", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Fleet offline share" })); metricSubmit();
  await screen.findByRole("cell", { name: "33.333333" });
  let resolve!: (response: Response) => void;
  handle = () => new Promise<Response>(r => { resolve = r; });
  fireEvent.change(screen.getByLabelText("Metric start date"), { target: { value: "2026-01-02" } });
  fireEvent.click(screen.getByRole("button", { name: "Load daily metric" }));
  await waitFor(() => expect(resolve).toBeDefined());
  fireEvent.click(screen.getByRole("button", { name: "Log out" }));
  role = "viewer"; handle = url => json(url.startsWith("/api/metrics/") ? { rows: [] } : { datasets: [] }); await login();
  await act(async () => resolve(json({ rows: [day] })));
  expect(screen.queryByRole("cell")).not.toBeInTheDocument();
  expect(screen.queryByText("33.333333%")).not.toBeInTheDocument();
});

test("navigation during admission retains one request and accepted tracking", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  let resolve!: (value: Response) => void;
  handle = (url, options) => options.method === "POST" ? new Promise<Response>(r => { resolve = r; }) : url.includes("/refresh/") ? json({ ...pending, status: "running" }) : json({ datasets: [] });
  refreshSubmit();
  await waitFor(() => expect(resolve).toBeTypeOf("function"));
  fireEvent.click(screen.getByRole("button", { name: "Datasets" }));
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(screen.getByRole("button", { name: "Starting refresh…" })).toBeDisabled();
  await act(async () => resolve(json(pending, 202)));
  expect(await screen.findByText("Job ID: local-job")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Datasets" }));
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(screen.getByText("Job ID: local-job")).toBeVisible();
  expect(calls.filter(c => c.url === "/api/refresh" && c.options.method === "POST")).toHaveLength(1);
});

test.each([409, 0])("lost admission %s recovers the active job", async status => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  handle = (url, options) => {
    if (options.method === "POST") { if (!status) throw new TypeError("lost response"); return json({}, status); }
    return json({ ...pending, status: "running" });
  };
  refreshSubmit(); await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "Start refresh" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Find active refresh" }));
  expect(await screen.findByText("Job ID: local-job")).toBeVisible();
  expect(calls.some(c => c.url === "/api/refresh/active")).toBe(true);
});

test("404 offers explicit unknown-outcome reset without claiming failure", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  handle = (_, options) => options.method === "POST" ? json(pending, 202) : json({}, 404);
  refreshSubmit();
  expect(await screen.findByRole("alert")).toHaveTextContent("Publication outcome is unknown");
  expect(screen.getByRole("button", { name: "Refresh outcome unknown" })).toBeDisabled();
  expect(screen.queryByText("Not published")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Clear unavailable job tracking" }));
  expect(screen.getByRole("button", { name: "Start refresh" })).toBeEnabled();
  expect(screen.queryByText("Job ID: local-job")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Start refresh" }));
  await waitFor(() => expect(calls.filter(c => c.url === "/api/refresh" && c.options.method === "POST")).toHaveLength(2));
});

test.each([0, 503])("transient status failure %s keeps tracking and blocks admission", async status => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  handle = (_, options) => {
    if (options.method === "POST") return json(pending, 202);
    if (!status) throw new TypeError("temporary");
    return json({}, status);
  };
  refreshSubmit(); await screen.findByRole("button", { name: "Retry status" });
  expect(screen.getByText("Job ID: local-job")).toBeVisible();
  expect(screen.getByRole("button", { name: "Refresh in progress…" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Clear unavailable job tracking" })).not.toBeInTheDocument();
});

test.each([true, false])("bootstrap date clears only on confirmed publication %s", async published => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  handle = (_, options) => options.method === "POST" ? json(pending, 202) : json({ ...pending,
    status: published ? "succeeded" : "failed", published, outcome: published ? "published" : null });
  refreshSubmit(); await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(published ? "succeeded" : "failed"));
  const start = screen.getByLabelText("First publication start date (optional)");
  await waitFor(() => expect(start).toHaveValue(published ? "" : "2026-01-01"));
  fireEvent.click(screen.getByRole("button", { name: "Start refresh" }));
  await waitFor(() => expect(calls.filter(c => c.url === "/api/refresh" && c.options.method === "POST")).toHaveLength(2));
  const body = JSON.parse(calls.filter(c => c.url === "/api/refresh" && c.options.method === "POST")[1].options.body as string);
  expect(body.start_date).toBe(published ? undefined : "2026-01-01");
});

test("late admission response cannot restore a previous session's tracking", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  let resolve!: (response: Response) => void;
  handle = (_, options) => options.method === "POST" ? new Promise<Response>(r => { resolve = r; }) : json({ datasets: [] });
  refreshSubmit(); await waitFor(() => expect(resolve).toBeTypeOf("function"));
  fireEvent.click(screen.getByRole("button", { name: "Log out" }));
  await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await act(async () => resolve(json(pending, 202)));
  expect(screen.queryByText("Job ID: local-job")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Start refresh" })).toBeEnabled();
});

test("no active job recovery does not claim the lost job failed", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  handle = (_, options) => options.method === "POST" ? json({}, 409) : json(null);
  refreshSubmit(); await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "Find active refresh" }));
  expect(await screen.findByText(/No active job is known/)).toHaveTextContent("publication outcome remains unknown");
  expect(screen.getByRole("button", { name: "Start refresh" })).toBeEnabled();
});

test("late active-job lookup cannot restore tracking after logout", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  let resolve!: (value: Response) => void;
  handle = () => new Promise<Response>(r => { resolve = r; });
  fireEvent.click(screen.getByRole("button", { name: "Find active refresh" }));
  await waitFor(() => expect(resolve).toBeTypeOf("function"));
  fireEvent.click(screen.getByRole("button", { name: "Log out" }));
  role = "viewer"; handle = () => json({ datasets: [] }); await login();
  await act(async () => resolve(json(pending)));
  expect(screen.queryByText("Job ID: local-job")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Find active refresh" })).not.toBeInTheDocument();
});

test("a no-op outcome does not claim a bootstrap publication", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  handle = (_, options) => options.method === "POST" ? json(pending, 202) : json({ ...pending,
    status: "succeeded", published: false, outcome: "no_op" });
  refreshSubmit(); await screen.findByText("Publication: No changes needed");
  expect(screen.getByLabelText("First publication start date (optional)")).toHaveValue("2026-01-01");
});


test("first publication date is optional, initially empty, and explains existing snapshots", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  const start = screen.getByLabelText("First publication start date (optional)");
  expect(start).toHaveValue("");
  expect(start).not.toBeRequired();
  const help = "Required only when creating the first production snapshot. If data was already published through the application or CLI, leave this blank; subsequent refreshes derive their starting point from published watermarks.";
  expect(screen.getByText(help)).toBeVisible();
  expect(start).toHaveAccessibleDescription(help);
});
