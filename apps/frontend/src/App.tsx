import { useState, type FormEvent } from "react";
import { SessionProvider, useSession } from "@/components/session";
import { Button } from "@/components/ui/button";
import { Reconciliation } from "@/components/reconciliation";
import { Datasets } from "@/components/datasets";
import { Refresh } from "@/components/refresh";
import { FleetShare } from "@/components/fleet-share";
import { SqlExplorer } from "@/components/sql-explorer";
import { cn } from "@/lib/utils";

function Login() {
  const { login, pending, message } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  function submit(event: FormEvent) {
    event.preventDefault();
    login(username.trim(), password);
    setPassword("");
  }
  const inputStyle = "mt-2 w-full rounded-md border border-slate-300 bg-white px-3 py-2 focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-100 disabled:bg-slate-100";
  return <main className="flex min-h-screen items-center justify-center bg-slate-50 px-5 py-12">
    <div className="w-full max-w-md rounded-xl border border-slate-200 bg-white p-7 shadow-sm sm:p-10">
      <p className="text-xs font-semibold uppercase tracking-widest text-blue-700">Nuclear outage analytics</p>
      <h1 className="mt-3 text-3xl font-semibold tracking-tight">Outage Explorer</h1>
      <p className="mt-3 text-sm text-slate-600">Sign in to explore the data available to your role.</p>
      {message && <p role="alert" className="mt-5 rounded-md bg-amber-50 p-3 text-sm text-amber-900">{message}</p>}
      <form onSubmit={submit} className="mt-7 space-y-5" aria-label="Sign in" aria-busy={pending}>
        <div><label htmlFor="username" className="text-sm font-medium">Username</label>
          <input id="username" name="username" autoComplete="username" required value={username} onChange={e => setUsername(e.target.value)} disabled={pending} className={inputStyle} /></div>
        <div><label htmlFor="password" className="text-sm font-medium">Password</label>
          <input id="password" name="password" type="password" autoComplete="current-password" required value={password} onChange={e => setPassword(e.target.value)} disabled={pending} className={inputStyle} /></div>
        <Button type="submit" disabled={pending} className="w-full">{pending ? "Signing in…" : "Sign in"}</Button>
        {pending && <p role="status" className="text-sm text-slate-600">Verifying your session…</p>}
      </form>
      <p className="mt-7 text-xs leading-5 text-slate-500">Local development accounts: viewer, analyst, admin. Use the passwords configured for your backend.</p>
    </div>
  </main>;
}

type Section = "datasets" | "sql" | "refresh" | "fleet" | "reconciliation";
function Shell() {
  const { user, logout } = useSession();
  const [section, setSection] = useState<Section>("datasets");
  if (!user) return <Login />;
  const navigation: { id: Section; label: string }[] = [
    { id: "datasets", label: user.role === "viewer" ? "National datasets" : "Datasets" },
    { id: "fleet", label: "Fleet offline share" },
    ...(user.role !== "viewer" ? [{ id: "sql" as const, label: "SQL Explorer" }] : []),
    ...(user.role !== "viewer" ? [{ id: "reconciliation" as const, label: "Reconciliation" }] : []),
    ...(user.role === "admin" ? [{ id: "refresh" as const, label: "Refresh" }] : []),
  ];
  const active = navigation.find(item => item.id === section) ?? navigation[0];
  return <div className="min-h-screen bg-slate-50 text-slate-900">
    <header className="border-b border-slate-200 bg-white px-5 py-5 sm:px-8">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-5">
        <div><p className="text-lg font-semibold tracking-tight">Outage Explorer</p><p className="text-xs text-slate-500">Nuclear outage analytics</p></div>
        <div className="flex items-center gap-4"><div className="text-right"><p className="text-sm font-medium">{user.username}</p><p className="text-xs capitalize text-slate-600">{user.role}</p></div><Button onClick={logout}>Log out</Button></div>
      </div>
    </header>
    <div className="mx-auto grid max-w-7xl gap-6 px-5 py-6 sm:px-8 md:grid-cols-[210px_1fr] md:py-10">
      <nav aria-label="Main navigation" className="flex flex-wrap content-start gap-2 md:flex-col">
        {navigation.map(item => <button key={item.id} onClick={() => setSection(item.id)} aria-current={active.id === item.id ? "page" : undefined}
          className={cn("rounded-md px-4 py-3 text-left text-sm font-medium focus-visible:outline-blue-600", active.id === item.id ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-200")}>{item.label}</button>)}
      </nav>
      <main className="min-w-0 rounded-xl border border-slate-200 bg-white p-6 sm:p-9">
        <p className="text-xs font-semibold uppercase tracking-widest text-slate-500">Workspace</p>
        <h1 className="mt-3 text-2xl font-semibold">{active.label}</h1>
        {active.id === "datasets" ? <Datasets /> : active.id === "sql" && user.role !== "viewer" ? <SqlExplorer /> : active.id === "reconciliation" && user.role !== "viewer" ? <Reconciliation /> : active.id === "fleet" ? <FleetShare /> : user.role === "admin" ? <Refresh /> : null}
      </main>
    </div>
  </div>;
}

export default function App() {
  return <>
    {import.meta.env.VITE_DEMO_MODE === "true" && <aside aria-label="Demonstration data" className="border-b border-amber-200 bg-amber-50 px-5 py-3 text-sm text-amber-950">
      <strong>Read-only real-data demo.</strong> Saved EIA observations; source completeness is unverified. Refresh cannot publish data.
    </aside>}
    <SessionProvider><Shell /></SessionProvider>
  </>;
}
