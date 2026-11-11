/** Thin fetch wrapper: same-origin credentials, CSRF for cookie sessions, optional dev headers. */

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export const DEV_AUTH = import.meta.env.VITE_DEV_AUTH === "1";

const LS_WS = "satchel.dev.workspace";
const LS_USER = "satchel.dev.user";

function ls(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

export const devAuth = {
  get workspace(): string {
    return ls(LS_WS) ?? import.meta.env.VITE_DEV_WORKSPACE ?? "";
  },
  get user(): string {
    return ls(LS_USER) ?? import.meta.env.VITE_DEV_USER ?? "";
  },
  set(workspace: string, user: string): void {
    try {
      localStorage.setItem(LS_WS, workspace);
      localStorage.setItem(LS_USER, user);
    } catch {
      /* storage unavailable: env defaults still apply */
    }
  },
  clear(): void {
    try {
      localStorage.removeItem(LS_WS);
      localStorage.removeItem(LS_USER);
    } catch {
      /* ignore */
    }
  },
};

let csrfToken: string | null = null;
export function setCsrf(token: string | null): void {
  csrfToken = token;
}

export function loginUrl(): string {
  return `/auth/login?next=${encodeURIComponent(location.pathname + location.search)}`;
}

type Query = Record<string, string | number | boolean | null | undefined>;

function qs(query?: Query): string {
  if (!query) return "";
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** Percent-encode each path segment but keep `/` (server routes use `{key:path}`). */
export function keyPath(key: string): string {
  return key.split("/").map(encodeURIComponent).join("/");
}

async function request<T>(
  method: string,
  path: string,
  opts: { query?: Query; body?: unknown; form?: FormData } = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.body !== undefined && !opts.form) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  if (DEV_AUTH) {
    if (devAuth.workspace) headers["X-Satchel-Workspace"] = devAuth.workspace;
    if (devAuth.user) headers["X-Satchel-User"] = devAuth.user;
  }
  const res = await fetch(path + qs(opts.query), {
    method,
    headers,
    credentials: "same-origin",
    body: opts.form ?? (opts.body === undefined ? undefined : JSON.stringify(opts.body)),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = (await res.json()) as { detail?: unknown };
      if (typeof j.detail === "string") detail = j.detail;
      else if (j.detail) detail = JSON.stringify(j.detail);
    } catch {
      /* non-JSON error body */
    }
    if (res.status === 401 && !DEV_AUTH) {
      location.assign(loginUrl());
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string, query?: Query) => request<T>("GET", path, { query }),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, { body: body ?? {} }),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, { body }),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, { body: body ?? {} }),
  del: <T = void>(path: string) => request<T>("DELETE", path),
  upload: <T>(path: string, form: FormData) => request<T>("POST", path, { form }),
};

export async function logout(): Promise<void> {
  await fetch("/auth/logout", {
    method: "POST",
    credentials: "same-origin",
    headers: csrfToken ? { "X-CSRF-Token": csrfToken } : {},
  }).catch(() => undefined);
  location.assign("/auth/login");
}
