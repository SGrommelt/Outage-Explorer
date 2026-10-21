import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiClient, ApiError, type User } from "@/lib/api";

interface Session {
  user: User | null;
  api: ApiClient;
  pending: boolean;
  message: string;
  login: (username: string, password: string) => void;
  logout: () => void;
}
const Context = createContext<Session | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [api] = useState(() => new ApiClient());
  const [user, setUser] = useState<User | null>(null);
  const [message, setMessage] = useState("");
  const controller = useRef<AbortController | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const queryClient = useQueryClient();
  function clearSession(reason = "") {
    controller.current?.abort();
    clearTimeout(timer.current);
    api.setToken(null);
    setUser(null);
    setMessage(reason);
    queryClient.clear();
  }
  useEffect(() => {
    api.onUnauthorized = () => clearSession("Your session has expired. Please sign in again.");
    return () => {
      controller.current?.abort();
      clearTimeout(timer.current);
      api.setToken(null);
      api.onUnauthorized = undefined;
    };
    // The client and query cache are stable for the provider's lifetime.
  }, [api, queryClient]);

  const mutation = useMutation({
    mutationFn: async ({ username, password }: { username: string; password: string }) => {
      controller.current?.abort();
      const attempt = new AbortController();
      controller.current = attempt;
      setMessage("");
      try {
        const result = await api.login(username, password, attempt.signal);
        if (attempt.signal.aborted) return;
        api.setToken(result.access_token);
        timer.current = setTimeout(() => clearSession("Your session has expired. Please sign in again."), result.expires_in * 1000);
        const identity = await api.me(attempt.signal);
        if (!attempt.signal.aborted) setUser(identity);
      } catch (error) {
        if (attempt.signal.aborted) return;
        clearSession(error instanceof ApiError && error.status === 401
          ? "Sign-in failed. Check your username and password and try again."
          : error instanceof ApiError ? error.message : "Unable to sign in. Please try again.");
      }
    },
    onSettled: (): void => { mutation.reset(); },
    // Never retain passwords in the shared mutation cache after completion.
    gcTime: 0,
  });
  return <Context.Provider value={{ user, api, message, pending: mutation.isPending,
    login: (username, password) => mutation.mutate({ username, password }),
    logout: () => clearSession(),
  }}>{children}</Context.Provider>;
}

export function useSession() {
  const session = useContext(Context);
  if (!session) throw new Error("Session provider is required.");
  return session;
}
