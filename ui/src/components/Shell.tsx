import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import { DEV_AUTH, devAuth, logout } from "@/api/client";
import { useTheme } from "@/lib/theme";
import { Dropdown, Icon, ICONS, Kbd, type MenuItem } from "@/components/ui";
import { Logo } from "@/components/Logo";
import { ALL_PAGES, DASHBOARD, DOCS_URL, NAV_GROUPS, type NavItem } from "@/components/nav";
import { CommandPaletteProvider, MOD_KEY, useCommandPalette } from "@/components/CommandPalette";

function NavRow({ n }: { n: NavItem }) {
  return (
    <NavLink
      to={n.to}
      className={({ isActive }) =>
        `flex h-10 items-center gap-3 rounded-md px-2.5 text-sm transition-colors md:h-9 ${
          isActive ? "bg-surface font-medium text-ink shadow-[inset_2px_0_0_rgb(var(--accent)),0_0_0_1px_rgb(var(--line))]" : "text-ink/75 hover:bg-sunken hover:text-ink"
        }`
      }
    >
      {({ isActive }) => (
        <>
          <Icon d={n.icon} size={17} className={isActive ? "text-accent" : "text-muted"} />
          {n.label}
        </>
      )}
    </NavLink>
  );
}

function initials(s: string | null | undefined) {
  const t = (s ?? "").trim();
  if (!t) return "?";
  const parts = t.split(/[\s@._-]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || "?";
}

function SearchRow() {
  const open = useCommandPalette();
  return (
    <button
      type="button"
      onClick={open}
      className="flex h-10 w-full items-center gap-3 rounded-md border border-line bg-surface px-2.5 text-sm text-muted transition-colors hover:border-ink/20 hover:text-ink md:h-9"
      aria-label="Search pages and actions"
      aria-keyshortcuts="Meta+K Control+K"
    >
      <Icon d={ICONS.search} size={16} />
      <span className="flex-1 text-left">Search</span>
      <Kbd>{MOD_KEY === "⌘" ? "⌘K" : "Ctrl K"}</Kbd>
    </button>
  );
}

function ShellBody() {
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me, staleTime: 60_000 });
  const [theme, toggleTheme] = useTheme();
  const [menu, setMenu] = useState(false);
  const loc = useLocation();
  const m = me.data;
  const here = ALL_PAGES.find((n) => loc.pathname.startsWith(n.to));

  useEffect(() => setMenu(false), [loc.pathname]);
  useEffect(() => {
    if (!menu) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setMenu(false);
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [menu]);

  const who = m ? (m.name ?? m.email ?? "dev (no user)") : "...";
  const userItems: MenuItem[] = [
    { label: theme === "dark" ? "Light theme" : "Dark theme", icon: theme === "dark" ? ICONS.sun : ICONS.moon, onSelect: toggleTheme },
    DEV_AUTH
      ? {
          label: "Switch dev workspace",
          icon: ICONS.logout,
          onSelect: () => {
            devAuth.clear();
            location.reload();
          },
        }
      : { label: "Sign out", icon: ICONS.logout, onSelect: () => void logout() },
  ];

  return (
    <div className="flex h-full flex-col md:p-3 lg:p-4">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-surface focus:px-3 focus:py-1.5">
        Skip to content
      </a>

      <div className="flex min-h-0 flex-1 flex-col bg-bg md:flex-row md:overflow-hidden md:rounded-2xl md:border md:border-line md:shadow-panel">
        <header className="flex h-12 shrink-0 items-center gap-3 border-b border-line bg-bg px-3 pt-[env(safe-area-inset-top)] md:hidden">
          <button className="btn btn-ghost px-2" onClick={() => setMenu(true)} aria-label="Open navigation" aria-expanded={menu} aria-controls="sidenav">
            <Icon d="M4 6h16M4 12h16M4 18h16" size={18} />
          </button>
          <Logo />
          {here && <span className="truncate text-sm text-muted">/ {here.label}</span>}
        </header>

        {menu && <div className="fixed inset-0 z-30 bg-black/40 md:hidden" onClick={() => setMenu(false)} aria-hidden />}

        <aside
          id="sidenav"
          className={`fixed inset-y-0 left-0 z-40 flex w-72 flex-col bg-bg transition-transform duration-200 md:static md:w-60 md:shrink-0 md:translate-x-0 md:border-r md:border-line/70 md:transition-none lg:w-64 ${
            menu ? "translate-x-0 shadow-xl" : "invisible -translate-x-full md:visible"
          }`}
        >
          <div className="flex h-16 shrink-0 items-center justify-between px-5 pt-[env(safe-area-inset-top)] md:pt-0">
            <Logo />
            <button className="btn btn-ghost px-2 md:hidden" onClick={() => setMenu(false)} aria-label="Close navigation">
              <Icon d={ICONS.close} size={16} strokeWidth={2} />
            </button>
          </div>

          <div className="space-y-3 px-3 pb-2">
            <div className="flex h-10 items-center gap-2.5 px-2" title={m?.workspace_slug}>
              <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-accent text-2xs font-semibold text-accent-ink" aria-hidden>
                {initials(m?.workspace_name)}
              </span>
              <span className="min-w-0 flex-1 truncate text-sm font-medium">{m?.workspace_name ?? "..."}</span>
              {m?.auth === "dev" && <span className="badge badge-warn">dev auth</span>}
            </div>
            <SearchRow />
          </div>

          <nav aria-label="Main" className="flex flex-1 flex-col overflow-y-auto px-3 pb-3 pt-1">
            <NavRow n={DASHBOARD} />
            {NAV_GROUPS.map((g) => (
              <div key={g.label} className="mt-5" role="group" aria-labelledby={`nav-${g.label}`}>
                <div className="mb-1.5 flex items-center gap-2.5 px-2.5">
                  <span className={`h-0.5 w-3.5 rounded-full ${g.dash}`} aria-hidden />
                  <span id={`nav-${g.label}`} className="label-caps">
                    {g.label}
                  </span>
                </div>
                <div className="flex flex-col gap-0.5">
                  {g.items.map((n) => (
                    <NavRow key={n.to} n={n} />
                  ))}
                </div>
              </div>
            ))}
          </nav>

          <div className="shrink-0 space-y-0.5 px-3 pt-2 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
            <a
              href={DOCS_URL}
              target="_blank"
              rel="noreferrer"
              className="flex h-10 items-center gap-3 rounded-md px-2.5 text-sm text-ink/75 hover:bg-sunken hover:text-ink md:h-9"
            >
              <Icon d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v16H5.5A1.5 1.5 0 0 1 4 18.5zM20 5.5A1.5 1.5 0 0 0 18.5 4H13v16h5.5a1.5 1.5 0 0 0 1.5-1.5z" size={17} className="text-muted" />
              <span className="flex-1">Docs</span>
              <Icon d="M7 17 17 7M9 7h8v8" size={14} className="text-muted" />
              <span className="sr-only">(opens in a new tab)</span>
            </a>
            <Dropdown
              label="Account menu"
              up
              align="left"
              className="w-full"
              menuClassName="flex h-11 w-full items-center gap-3 rounded-md px-2.5 text-left text-sm hover:bg-sunken"
              items={userItems}
              header={
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium">{who}</div>
                  <div className="truncate text-xs text-muted">{m ? `${m.email ?? "no email"} · ${m.role}` : ""}</div>
                </div>
              }
              trigger={
                <>
                  <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-sunken text-2xs font-semibold text-ink ring-1 ring-line" aria-hidden>
                    {initials(m?.name ?? m?.email)}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{who}</span>
                  </span>
                  <Icon d={ICONS.upDown} size={14} className="text-muted" />
                </>
              }
            />
          </div>
        </aside>

        <main id="main" className="min-w-0 flex-1 overflow-auto">
          <div className="mx-auto max-w-[1280px] px-4 py-5 sm:px-6 md:px-8 md:py-7 lg:px-10">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}

export function Shell() {
  return (
    <CommandPaletteProvider>
      <ShellBody />
    </CommandPaletteProvider>
  );
}
