import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, vi } from "vitest";
import App from "./App";
import { ApiClient, type Role } from "./lib/api";

const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const token = { access_token: "test-token", token_type: "bearer", expires_in: 600 };
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  client.setQueryData(["old-private-data"], "private");
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
  return client;
}
function submit() {
  fireEvent.change(screen.getByLabelText("Username"), { target: { value: "local-user" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "test-only-password" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}
function mockLogin(role: Role, expires = 600) {
  const fetcher = vi.fn().mockResolvedValueOnce(reply({ ...token, expires_in: expires })).mockResolvedValueOnce(reply({ username: "local-user", role }));
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

test("unauthenticated users see only the login form and make no data requests", () => {
  const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher); mount();
  expect(screen.getByRole("heading", { name: "Outage Explorer" })).toBeVisible();
  expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  expect(fetcher).not.toHaveBeenCalled();
});

test.each(["viewer", "analyst", "admin"] as const)("login identifies %s through /auth/me and limits navigation", async role => {
  const fetcher = mockLogin(role); mount(); submit();
  await screen.findByRole("button", { name: "Log out" });
  expect(screen.getByText("local-user")).toBeVisible();
  expect(screen.getByText(role, { exact: true })).toBeVisible();
  expect(screen.queryByRole("button", { name: "SQL Explorer" }) !== null).toBe(role !== "viewer");
  expect(screen.queryByRole("button", { name: "Refresh" }) !== null).toBe(role === "admin");
  expect(screen.getByRole("button", { name: role === "viewer" ? "National datasets" : "Datasets" })).toHaveAttribute("aria-current", "page");
  expect(fetcher.mock.calls[0][0]).toBe("/api/auth/login");
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ username: "local-user", password: "test-only-password" });
  expect(fetcher.mock.calls[1][0]).toBe("/api/auth/me");
  expect(fetcher.mock.calls[1][1].headers.get("Authorization")).toBe("Bearer test-token");
  if (role !== "viewer") {
    fireEvent.click(screen.getByRole("button", { name: "SQL Explorer" }));
    expect(screen.getByRole("heading", { name: "SQL Explorer" })).toBeVisible();
  }
  if (role === "admin") {
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(screen.getByRole("heading", { name: "Refresh" })).toBeVisible();
  }
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0);
});

test("logout locks the workspace and clears cached data", async () => {
  mockLogin("admin"); const client = mount(); submit();
  fireEvent.click(await screen.findByRole("button", { name: "Log out" }));
  expect(screen.getByRole("button", { name: "Sign in" })).toBeVisible();
  expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  expect(client.getQueryData(["old-private-data"])).toBeUndefined();
});

test("pending identity verification never opens protected views", async () => {
  let resolve!: (value: Response) => void;
  const identity = new Promise<Response>(r => { resolve = r; });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(reply(token)).mockReturnValueOnce(identity));
  mount(); submit();
  expect(await screen.findByRole("status")).toHaveTextContent("Verifying");
  expect(screen.getByRole("button", { name: "Signing in…" })).toBeDisabled();
  expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  await act(async () => { resolve(reply({ username: "local-user", role: "viewer" })); });
  await screen.findByRole("navigation");
});

test("invalid credentials show a safe error and allow retry", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(reply({ detail: "sensitive internal error" }, 401)));
  mount(); submit();
  expect(await screen.findByRole("alert")).toHaveTextContent("Check your username and password");
  expect(screen.queryByText(/sensitive internal/)).not.toBeInTheDocument();
  expect(screen.getByLabelText("Password")).toHaveValue("");
  await waitFor(() => expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled());
});

test.each(["login", "identity"])("network failure during %s keeps the workspace locked", async stage => {
  const fetcher = vi.fn();
  if (stage === "identity") fetcher.mockResolvedValueOnce(reply(token));
  fetcher.mockRejectedValueOnce(new TypeError("secret internal host"));
  vi.stubGlobal("fetch", fetcher); mount(); submit();
  expect(await screen.findByRole("alert")).toHaveTextContent("Unable to reach the backend");
  expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
});

test("identity 401 expires the session without showing protected views", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(reply(token)).mockResolvedValueOnce(reply({}, 401)));
  mount(); submit();
  expect(await screen.findByRole("alert")).toHaveTextContent("session has expired");
  expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
});

test("session lifetime expiry locks an open workspace", async () => {
  mockLogin("admin", 1); mount(); submit();
  await screen.findByRole("navigation");
  expect(await screen.findByRole("alert", {}, { timeout: 2000 })).toHaveTextContent("session has expired");
  expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
});

test("unrecognized identity role is rejected", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(reply(token)).mockResolvedValueOnce(reply({ username: "user", role: "superuser" })));
  mount(); submit();
  expect(await screen.findByRole("alert")).toHaveTextContent("invalid user");
  expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
});

test("client centralizes 401 handling and omits credentials after expiry", async () => {
  const api = new ApiClient("https://backend.example/api/"); api.setToken("old-token");
  const unauthorized = vi.fn(); api.onUnauthorized = unauthorized;
  const fetcher = vi.fn().mockResolvedValue(reply({}, 401)); vi.stubGlobal("fetch", fetcher);
  await expect(api.request("/auth/me")).rejects.toMatchObject({ status: 401 });
  await expect(api.request("/auth/me")).rejects.toMatchObject({ status: 401 });
  expect(unauthorized).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0][0]).toBe("https://backend.example/api/auth/me");
  expect(fetcher.mock.calls[1][1].headers.has("Authorization")).toBe(false);
});

test("a late 401 from an old session cannot expire a new session", async () => {
  let resolve!: (value: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(r => { resolve = r; })));
  const api = new ApiClient(); const unauthorized = vi.fn(); api.onUnauthorized = unauthorized;
  api.setToken("old"); const request = api.request("/auth/me"); api.setToken("new");
  resolve(reply({}, 401)); await expect(request).rejects.toMatchObject({ status: 401 });
  expect(unauthorized).not.toHaveBeenCalled();
});


test.each([
  new Response("not json", { status: 200 }),
  reply({ ...token, expires_in: 0 }),
  reply(null),
])("malformed login responses never unlock the application", async response => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(response));
  mount(); submit();
  expect(await screen.findByRole("alert")).toHaveTextContent("invalid");
  expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
});

test("server failures do not display backend response details", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(reply({ detail: "private configuration path" }, 503)));
  mount(); submit();
  expect(await screen.findByRole("alert")).toHaveTextContent("request could not be completed");
  expect(screen.queryByText(/private configuration/)).not.toBeInTheDocument();
});
