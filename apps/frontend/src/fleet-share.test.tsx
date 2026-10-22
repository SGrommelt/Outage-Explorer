import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, vi } from "vitest";
import { FleetShare } from "./components/fleet-share";
import { DataTable } from "./components/data-table";
import { formatDecimal, formatPercent } from "./lib/format";
import { ApiError } from "./lib/api";

const request = vi.fn();
vi.mock("./components/session", () => ({ useSession: () => ({ api: { request }, user: { username: "viewer", role: "viewer" } }) }));
const day = { outage_date: "2024-02-29", total_fleet_capacity_mw: "100056.700000", offline_capacity_mw: "19859.767000", fleet_offline_share_pct: "19.848513", status: "ok" };
function setup() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><FleetShare /></QueryClientProvider>);
}
beforeEach(() => { request.mockReset(); request.mockResolvedValue({ rows: [day] }); });

test("automatically loads backend-derived dates in one request without a today assumption", async () => {
  setup();
  const card = await screen.findByLabelText("Latest observed fleet offline share");
  expect(card).toHaveTextContent("19.848513%");
  expect(card).toHaveTextContent("2024-02-29");
  expect(card).toHaveTextContent("19,859.767 MW offline of 100,056.7 MW capacity");
  expect(card).toHaveTextContent("not live telemetry");
  await waitFor(() => expect(screen.getByLabelText("Metric start date")).toHaveValue("2024-02-29"));
  expect(screen.getByLabelText("Metric end date")).toHaveValue("2024-02-29");
  expect(request).toHaveBeenCalledTimes(1);
  expect(request.mock.calls[0][0]).toBe("/metrics/fleet-offline-share");
});

test("chart shows true zero and breaks around invalid and missing days", async () => {
  request.mockResolvedValue({ rows: [
    { ...day, outage_date: "2024-02-25", fleet_offline_share_pct: "0.000000" },
    { ...day, outage_date: "2024-02-26", fleet_offline_share_pct: null, status: "missing_date" },
    { ...day, outage_date: "2024-02-27" },
    { ...day, outage_date: "2024-02-28", fleet_offline_share_pct: null, status: "zero_capacity" },
    day, { ...day, outage_date: "2024-03-01" },
  ] });
  setup();
  const chart = await screen.findByRole("figure", { name: /Daily national/ });
  expect(within(chart).getByRole("img")).toHaveAccessibleDescription(/Gaps indicate missing or invalid percentages and are not connected/);
  expect(within(chart).getAllByTestId("trend-point")).toHaveLength(4);
  expect(within(chart).getAllByTestId("trend-segment")).toHaveLength(1);
  expect(within(chart).getByText("2024-02-25: 0%", { selector: "title" })).toBeInTheDocument();
  expect(screen.getByRole("table")).toHaveTextContent("zero_capacity");
  expect(within(chart).getByRole("img")).toHaveClass("w-full", "h-auto");
});

test("latest invalid observation is unavailable instead of silently falling back", async () => {
  request.mockResolvedValue({ rows: [day, { ...day, outage_date: "2024-03-01", status: "zero_capacity", fleet_offline_share_pct: null }] });
  setup();
  const card = await screen.findByLabelText("Latest observed fleet offline share");
  expect(card).toHaveTextContent("2024-03-01");
  expect(card).toHaveTextContent("Unavailable");
  expect(card).not.toHaveTextContent("19.848513%");
});

test("editing dates clears previous results and submits explicit bounded dates", async () => {
  setup(); await screen.findByLabelText("Latest observed fleet offline share");
  await waitFor(() => expect(screen.getByLabelText("Metric start date")).toHaveValue("2024-02-29"));
  fireEvent.change(screen.getByLabelText("Metric start date"), { target: { value: "2024-02-01" } });
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Load daily metric" }));
  await screen.findByRole("table");
  expect(request.mock.calls[1][0]).toBe("/metrics/fleet-offline-share?start_date=2024-02-01&end_date=2024-02-29");
});

test("invalid reversed dates prevent fetching", async () => {
  setup(); await screen.findByRole("table");
  await waitFor(() => expect(screen.getByLabelText("Metric end date")).toHaveValue("2024-02-29"));
  fireEvent.change(screen.getByLabelText("Metric start date"), { target: { value: "2025-02-01" } });
  fireEvent.click(screen.getByRole("button", { name: "Load daily metric" }));
  expect(screen.getByRole("alert")).toHaveTextContent("at most 366 days");
  expect(request).toHaveBeenCalledTimes(1);
});

test("loading contains no stale values", () => {
  request.mockReturnValue(new Promise(() => {})); setup();
  expect(screen.getByRole("status")).toHaveTextContent("Loading daily metric");
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});

test("empty history shows a clear unavailable state without a chart", async () => {
  request.mockResolvedValue({ rows: [] }); setup();
  expect(await screen.findByText("No valid percentages are available for this window.")).toBeVisible();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Latest observed fleet offline share")).not.toBeInTheDocument();
});

test("API failure is safe and retry loads the metric", async () => {
  request.mockRejectedValueOnce(new ApiError(503, "private path")); setup();
  expect(await screen.findByRole("alert")).not.toHaveTextContent("private");
  fireEvent.click(screen.getByRole("button", { name: "Retry daily metric" }));
  await screen.findByLabelText("Latest observed fleet offline share");
});

test("shared table formatting preserves identifiers and micro-MW gaps", () => {
  render(<DataTable caption="Exact values" columns={[{ name: "capacity_mw", type: "DECIMAL(18,6)" }, { name: "gap_mw", type: "DECIMAL(18,6)" }, { name: "facility_id", type: "VARCHAR" }]} rows={[["100056.700000", "-0.000001", "00123"]]} />);
  expect(screen.getByRole("cell", { name: "100,056.7" })).toBeVisible();
  expect(screen.getByRole("cell", { name: "-0.000001" })).toBeVisible();
  expect(screen.getByRole("cell", { name: "00123" })).toBeVisible();
});

test("decimal and percentage formatting never rounds a nonzero value to zero", () => {
  expect(formatDecimal("999999999999.000001")).toBe("999,999,999,999.000001");
  expect(formatDecimal("0.000001")).toBe("0.000001");
  expect(formatPercent("0.000000")).toBe("0%");
  expect(formatPercent(null)).toBe("Unavailable");
});
