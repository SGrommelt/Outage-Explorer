export type Role = "viewer" | "analyst" | "admin";
export interface User { username: string; role: Role }
interface LoginResponse { access_token: string; token_type: string; expires_in: number }

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) { super(message); }
}

/** Token lives only in this client instance; never persisted or placed in a URL. */
export class ApiClient {
  private token: string | null = null;
  onUnauthorized: (() => void) | undefined;
  constructor(private readonly baseUrl = import.meta.env.VITE_API_BASE_URL || "/api") {}
  setToken(token: string | null) { this.token = token; }

  async request<T>(path: string, options: RequestInit = {}, authenticated = true): Promise<T> {
    const token = authenticated ? this.token : null;
    const headers = new Headers(options.headers);
    if (token) headers.set("Authorization", `Bearer ${token}`);
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl.replace(/\/$/, "")}${path}`, {
        ...options, headers, credentials: "omit", cache: "no-store",
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new ApiError(0, "Unable to reach the backend. Check your connection and try again.");
    }
    if (!response.ok) {
      // A stale request must never log out a newer session.
      if (response.status === 401 && token && token === this.token) {
        this.token = null;
        this.onUnauthorized?.();
      }
      throw new ApiError(response.status, response.status === 401
        ? "Authentication failed." : "The request could not be completed. Please try again.");
    }
    try { return await response.json() as T; }
    catch { throw new ApiError(502, "The backend returned an invalid response. Please try again."); }
  }

  async login(username: string, password: string, signal: AbortSignal): Promise<LoginResponse> {
    const result = await this.request<LoginResponse>("/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }), signal,
    }, false);
    if (typeof result?.access_token !== "string" || !result.access_token ||
        result.token_type !== "bearer" || !Number.isFinite(result.expires_in) || result.expires_in <= 0) {
      throw new ApiError(502, "The backend returned an invalid session. Please try again.");
    }
    return result;
  }

  async me(signal: AbortSignal): Promise<User> {
    const user = await this.request<User>("/auth/me", { signal });
    if (typeof user?.username !== "string" || !user.username ||
        !["viewer", "analyst", "admin"].includes(user.role)) {
      throw new ApiError(502, "The backend returned an invalid user. Please try again.");
    }
    return user;
  }
}
