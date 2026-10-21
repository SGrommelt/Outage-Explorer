import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, vi } from "vitest";
import App from "./App";
import type { Dataset } from "./lib/data";

const national: Dataset = { name: "national_outages", description: "Fleet totals", columns: [{ name: "outage_date", type: "DATE" }, { name: "outage_mw", type: "DECIMAL(18,6)" }] };
const facility: Dataset = { name: "facility_outages", description: "Facility totals", columns: [...national.columns, { name: "facility_id", type: "VARCHAR" }] };
const generator: Dataset = { name: "generator_outages", description: "Generator totals", columns: [...facility.columns, { name: "generator_id", type: "VARCHAR" }] };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
let role = "analyst";
let requests: { url: string; options: RequestInit }[];
let handle: (url: URL, options: RequestInit) => Response | Promise<Response>;
function setup() {
  requests = []; role = "analyst";
  handle = (url) => {
    if (url.pathname === "/api/datasets") return json({ datasets: role === "viewer" ? [national] : [national, facility, generator] });
    const page = Number(url.searchParams.get("page") ?? 1);
    return json({ dataset: url.pathname.split("/")[3], page, page_size: 50, total: 101,
      rows: [{ outage_date: `2026-01-0${page}`, outage_mw: "123.123456", facility_id: "F001", generator_id: "G1" }] });
  };
  const fetcher = vi.fn(async (input: string, options: RequestInit) => {
    requests.push({ url: input, options });
    if (input === "/api/auth/login") return json({ access_token: `token-${role}`, token_type: "bearer", expires_in: 600 });
    if (input === "/api/auth/me") return json({ username: role, role });
    return handle(new URL(input, "http://localhost"), options);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><App /></QueryClientProvider>);
}
async function login() {
  fireEvent.change(screen.getByLabelText("Username"), { target: { value: role } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "synthetic-test-password" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  await screen.findByLabelText("Dataset");
}
afterEach(() => vi.unstubAllGlobals());

test("catalog metadata and generic previews support all returned datasets", async () => {
  setup(); await login();
  expect(screen.getAllByRole("option").map(option => option.textContent)).toContain("generator_outages");
  expect(screen.getByText("Fleet totals")).toBeVisible();
  fireEvent.click(screen.getByText("Columns and data types"));
  expect(screen.getAllByText("DECIMAL(18,6)").length).toBeGreaterThan(0);
  await screen.findByRole("cell", { name: "123.123456" });
  expect(screen.queryByLabelText("Facility ID")).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Dataset"), { target: { value: "generator_outages" } });
  expect(await screen.findByText("Generator totals")).toBeVisible();
  await screen.findByRole("cell", { name: "G1" });
  expect(screen.getByLabelText("Facility ID")).toBeVisible();
});

test("date and facility filters are sent to backend and pagination resets", async () => {
  setup(); await login(); await screen.findByRole("cell", { name: "2026-01-01" });
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  await screen.findByRole("cell", { name: "2026-01-02" });
  expect(screen.getByText(/101 matching rows · Page 2 of 3/)).toBeVisible();
  fireEvent.change(screen.getByLabelText("Start date"), { target: { value: "2026-01-01" } });
  await waitFor(() => expect(requests.at(-1)?.url).toContain("page=1"));
  fireEvent.change(screen.getByLabelText("End date"), { target: { value: "2026-01-31" } });
  await waitFor(() => expect(requests.at(-1)?.url).toContain("end_date=2026-01-31"));
  fireEvent.change(screen.getByLabelText("Dataset"), { target: { value: "facility_outages" } });
  await screen.findByLabelText("Facility ID");
  fireEvent.change(screen.getByLabelText("Facility ID"), { target: { value: "F001" } });
  await waitFor(() => expect(requests.at(-1)?.url).toContain("facility_id=F001"));
  fireEvent.change(screen.getByLabelText("Rows per page"), { target: { value: "200" } });
  await waitFor(() => expect(requests.at(-1)?.url).toContain("page_size=200"));
  await waitFor(() => expect(screen.getByRole("button", { name: "Next" })).toBeDisabled());
  expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
});

test("changed filters remove old rows and late responses cannot replace current rows", async () => {
  setup(); await login(); await screen.findByRole("cell", { name: "123.123456" });
  let resolve!: (response: Response) => void;
  handle = () => new Promise<Response>(r => { resolve = r; });
  fireEvent.change(screen.getByLabelText("Start date"), { target: { value: "2026-02-01" } });
  expect(screen.queryByRole("cell")).not.toBeInTheDocument();
  await waitFor(() => expect(resolve).toBeDefined());
  handle = () => json({ page: 1, page_size: 50, total: 0, rows: [] });
  fireEvent.change(screen.getByLabelText("Start date"), { target: { value: "2026-03-01" } });
  await screen.findByText("No rows match this request.");
  await act(async () => resolve(json({ page: 1, page_size: 50, total: 1, rows: [{ outage_mw: "stale" }] })));
  expect(screen.queryByText("stale")).not.toBeInTheDocument();
});

test("invalid date ranges clear data and prevent invalid requests", async () => {
  setup(); await login(); await screen.findByRole("cell", { name: "123.123456" });
  fireEvent.change(screen.getByLabelText("Start date"), { target: { value: "2026-02-01" } });
  fireEvent.change(screen.getByLabelText("End date"), { target: { value: "2026-01-01" } });
  expect(screen.getByRole("alert")).toHaveTextContent("Start date must be");
  expect(screen.queryByRole("cell")).not.toBeInTheDocument();
  expect(requests.some(request => request.url.includes("end_date=2026-01-01"))).toBe(false);
});

test.each([403, 503, 400])("preview errors (%s) are safe and hide previous rows", async status => {
  setup(); await login(); await screen.findByRole("cell", { name: "123.123456" });
  handle = () => json({ detail: "secret filesystem path" }, status);
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  expect(await screen.findByRole("alert")).not.toHaveTextContent("secret filesystem");
  expect(screen.queryByRole("cell")).not.toBeInTheDocument();
});

test("SQL executes server requests, preserves types/nulls and reports truncation", async () => {
  setup(); await login();
  fireEvent.click(screen.getByRole("button", { name: "SQL Explorer" }));
  fireEvent.click(await screen.findByRole("button", { name: "Use example query" }));
  expect(screen.getByLabelText("SQL statement")).toHaveValue("SELECT * FROM national_outages ORDER BY outage_date DESC LIMIT 50");
  handle = (url, options) => {
    expect(url.pathname).toBe("/api/query"); expect(options.method).toBe("POST");
    expect(JSON.parse(options.body as string).sql).toContain("SELECT");
    return json({ columns: national.columns, rows: [["2026-01-01", "123.123456"], ["2026-01-02", null]], row_count: 2, row_limit: 200, truncated: true });
  };
  fireEvent.click(screen.getByRole("button", { name: "Run query" }));
  await screen.findByRole("cell", { name: "123.123456" });
  expect(screen.getByRole("cell", { name: "NULL" })).toBeVisible();
  expect(screen.getByText(/Truncated at the 200-row/)).toBeVisible();
  fireEvent.change(screen.getByLabelText("SQL statement"), { target: { value: "SELECT outage_mw FROM national_outages" } });
  expect(screen.queryByRole("cell")).not.toBeInTheDocument();
});

test.each([400, 403, 503])("SQL errors (%s) clear results and show safe messages", async status => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "SQL Explorer" }));
  fireEvent.change(screen.getByLabelText("SQL statement"), { target: { value: "SELECT * FROM national_outages" } });
  handle = () => json({ detail: "secret raw query path" }, status);
  fireEvent.click(screen.getByRole("button", { name: "Run query" }));
  expect(await screen.findByRole("alert")).not.toHaveTextContent("secret raw");
  expect(screen.queryByRole("cell")).not.toBeInTheDocument();
});

test("catalog failures show unavailable state without dataset details", async () => {
  setup(); handle = () => json({}, 503); await loginFailure();
  expect(await screen.findByRole("alert")).toHaveTextContent("Published data is unavailable");
  expect(screen.queryByLabelText("Dataset")).not.toBeInTheDocument();
});
async function loginFailure() {
  fireEvent.change(screen.getByLabelText("Username"), { target: { value: "analyst" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "test-password" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

test("switching to Viewer removes facility details and SQL access", async () => {
  setup(); await login();
  fireEvent.change(screen.getByLabelText("Dataset"), { target: { value: "facility_outages" } });
  await screen.findByRole("cell", { name: "F001" });
  fireEvent.click(screen.getByRole("button", { name: "Log out" }));
  role = "viewer"; await login();
  expect(screen.queryByRole("option", { name: "facility_outages" })).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Facility ID")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "SQL Explorer" })).not.toBeInTheDocument();
  expect(screen.queryByText("F001")).not.toBeInTheDocument();
});

test("SQL results from an old account never survive logout", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "SQL Explorer" }));
  fireEvent.change(screen.getByLabelText("SQL statement"), { target: { value: "SELECT * FROM facility_outages" } });
  const defaultHandler = handle;
  handle = url => url.pathname === "/api/query" ? json({ columns: facility.columns, rows: [["2026-01-01", "999.000000", "PRIVATE-FACILITY"]], row_count: 1, row_limit: 200, truncated: false }) : defaultHandler(url, {});
  fireEvent.click(screen.getByRole("button", { name: "Run query" }));
  await screen.findByRole("cell", { name: "PRIVATE-FACILITY" });
  fireEvent.click(screen.getByRole("button", { name: "Log out" }));
  role = "viewer"; await login();
  expect(screen.queryByText("PRIVATE-FACILITY")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("SQL statement")).not.toBeInTheDocument();
});

test("editing pending SQL discards a late result", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "SQL Explorer" }));
  let resolve!: (value: Response) => void;
  handle = () => new Promise<Response>(r => { resolve = r; });
  fireEvent.change(screen.getByLabelText("SQL statement"), { target: { value: "SELECT * FROM national_outages" } });
  fireEvent.click(screen.getByRole("button", { name: "Run query" }));
  await waitFor(() => expect(resolve).toBeDefined());
  fireEvent.change(screen.getByLabelText("SQL statement"), { target: { value: "SELECT outage_date FROM national_outages" } });
  await act(async () => resolve(json({ columns: national.columns, rows: [["stale-result", null]], row_count: 1, row_limit: 200, truncated: false })));
  expect(screen.queryByText("stale-result")).not.toBeInTheDocument();
});

test("empty SQL results display headers and zero rows", async () => {
  setup(); await login(); fireEvent.click(screen.getByRole("button", { name: "SQL Explorer" }));
  fireEvent.change(screen.getByLabelText("SQL statement"), { target: { value: "SELECT * FROM national_outages LIMIT 0" } });
  handle = () => json({ columns: national.columns, rows: [], row_count: 0, row_limit: 200, truncated: false });
  fireEvent.click(screen.getByRole("button", { name: "Run query" }));
  expect(await screen.findByText("No rows match this request.")).toBeVisible();
  expect(screen.getByText(/0 rows returned/)).toBeVisible();
  expect(screen.getByRole("columnheader", { name: /outage_date/ })).toBeVisible();
});

test("catalog network failures are recoverable", async () => {
  setup(); handle = () => Promise.reject(new TypeError("internal hostname")); await loginFailure();
  expect(await screen.findByRole("alert")).toHaveTextContent("Unable to reach the backend");
  handle = () => json({ datasets: [] });
  fireEvent.click(screen.getByRole("button", { name: "Retry catalog" }));
  expect(await screen.findByText("No datasets are available for your account.")).toBeVisible();
});
