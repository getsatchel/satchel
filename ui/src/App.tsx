import { useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { DEV_AUTH, devAuth, setCsrf } from "./api/client";
import { endpoints } from "./api/endpoints";
import { Shell } from "./components/Shell";
import { Logo } from "./components/Logo";
import { ErrorBox, Loading } from "./components/ui";
import { Dashboard } from "./pages/Dashboard";
import { Inbox } from "./pages/Inbox";
import { Library } from "./pages/Library";
import { ItemDetailPage } from "./pages/ItemDetail";
import { ItemNew } from "./pages/ItemNew";
import { Changes } from "./pages/Changes";
import { Releases } from "./pages/Releases";
import { Sessions } from "./pages/Sessions";
import { SessionInspector } from "./pages/SessionInspector";
import { Observability } from "./pages/Observability";
import { Catalog } from "./pages/Catalog";
import { SourceDetail } from "./pages/SourceDetail";
import { Sources } from "./pages/Sources";
import { Settings } from "./pages/Settings";
import { EvalComparePage, EvalRunPage, Evals } from "./pages/Evals";
import { Jobs } from "./pages/Jobs";

function DevLogin({ onDone }: { onDone: () => void }) {
  const [ws, setWs] = useState(devAuth.workspace);
  const [user, setUser] = useState(devAuth.user);
  return (
    <div className="grid h-full place-items-center">
      <form
        className="card w-[calc(100%-2rem)] max-w-sm space-y-3 p-5"
        onSubmit={(e) => {
          e.preventDefault();
          devAuth.set(ws.trim(), user.trim());
          onDone();
        }}
      >
        <div className="mb-1"><Logo /></div>
        <h1 className="text-lg font-semibold">Dev sign-in</h1>
        <p className="text-sm text-muted">
          Dev header auth is on (VITE_DEV_AUTH=1). The server must run with SATCHEL_DEV_AUTH=1.
        </p>
        <label className="block">
          <span className="label">Workspace slug</span>
          <input className="input w-full" value={ws} onChange={(e) => setWs(e.target.value)} autoFocus required />
        </label>
        <label className="block">
          <span className="label">User id (UUID, optional; without one you act as explorer)</span>
          <input className="input mono w-full" value={user} onChange={(e) => setUser(e.target.value)} />
        </label>
        <button className="btn btn-primary" type="submit">
          Continue
        </button>
      </form>
    </div>
  );
}

function Gate() {
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me, retry: false });
  useEffect(() => {
    if (me.data) setCsrf(me.data.csrf_token);
  }, [me.data]);
  if (me.isPending) return <Loading label="Signing in" />;
  if (me.isError) {
    // Non-dev 401s redirect to /auth/login inside the client; here we only see dev-mode or hard errors.
    return (
      <div className="mx-auto max-w-lg p-8">
        <ErrorBox error={me.error} />
        {DEV_AUTH && (
          <button
            className="btn mt-3"
            onClick={() => {
              devAuth.clear();
              location.reload();
            }}
          >
            Change dev workspace
          </button>
        )}
      </div>
    );
  }
  return <Shell />;
}

export function App() {
  const [ready, setReady] = useState(!DEV_AUTH || Boolean(devAuth.workspace));
  if (!ready) return <DevLogin onDone={() => setReady(true)} />;
  return (
    <Routes>
      <Route element={<Gate />}>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard" element={<Dashboard />} />
        <Route path="inbox" element={<Inbox />} />
        <Route path="library" element={<Library />} />
        <Route path="library/new" element={<ItemNew />} />
        <Route path="library/item/*" element={<ItemDetailPage />} />
        <Route path="changes" element={<Changes />} />
        <Route path="releases" element={<Releases />} />
        <Route path="sessions" element={<Sessions />} />
        <Route path="sessions/:id" element={<SessionInspector />} />
        <Route path="observability" element={<Observability />} />
        <Route path="catalog" element={<Catalog />} />
        <Route path="sources" element={<Sources />} />
        <Route path="sources/:name" element={<SourceDetail />} />
        <Route path="settings" element={<Settings />} />
        <Route path="evals" element={<Evals />} />
        <Route path="jobs" element={<Jobs />} />
        <Route path="evals/runs/:id" element={<EvalRunPage />} />
        <Route path="evals/compare" element={<EvalComparePage />} />
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Route>
    </Routes>
  );
}
