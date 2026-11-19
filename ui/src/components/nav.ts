import { ICONS } from "@/components/ui";
import { atLeast, type Role } from "@/lib/role";

/** The documentation site. Its pages are the repository's `docs/` folder, at `#/docs/<page>`. */
export const DOCS_SITE = "https://getsatchel.dev";

/**
 * A page of the documentation site: the path of a file under `docs/` without `.md`
 * (`integrations/claude` for docs/integrations/claude.md). No page: the docs overview.
 */
export function docsUrl(page = "overview"): string {
  return `${DOCS_SITE}/#/docs/${page}`;
}

export interface NavItem {
  to: string;
  label: string;
  icon: string;
  /** Extra words the command palette matches on. */
  keywords?: string;
  /** Lowest role the page is for. Everyone else does not see it (the server refuses them too). */
  min?: Role;
}

export interface NavGroup {
  label: string;
  /** Tailwind background class for the group's short dash. */
  dash: string;
  items: NavItem[];
}

export const DASHBOARD: NavItem = { to: "/dashboard", label: "Dashboard", icon: ICONS.dashboard, keywords: "home overview" };

export const NAV_GROUPS: NavGroup[] = [
  {
    label: "Curate",
    dash: "bg-accent/60",
    items: [
      { to: "/inbox", label: "Inbox", icon: ICONS.inbox, keywords: "suggestions proposals review", min: "curator" },
      { to: "/library", label: "Library", icon: "M4 4h6v16H4zM12 4h3v16h-3zM17 6l3-1 2 15-3 1z", keywords: "items context docs glossary" },
      { to: "/agent", label: "Agent instructions", icon: "M4 5h16v11H9l-5 4zM8 9h8M8 12h5", keywords: "behaviour behavior prompt skill tone format answer style", min: "curator" },
      { to: "/releases", label: "Releases", icon: "M12 3l9 5v8l-9 5-9-5V8zM12 12l9-4M12 12v9M12 12L3 8", keywords: "rollback versions changes preview publish unpublished drafts history", min: "curator" },
    ],
  },
  {
    label: "Observe",
    dash: "bg-info/55",
    items: [
      { to: "/sessions", label: "Sessions", icon: "M4 6h16M4 12h10M4 18h13", keywords: "agents runs replay questions conversations usage observability" },
      { to: "/evals", label: "Evals", icon: "M9 11l3 3 8-8M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9", keywords: "suites runs tests", min: "curator" },
    ],
  },
  {
    label: "Data",
    dash: "bg-good/55",
    items: [
      { to: "/catalog", label: "Catalog", icon: "M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3zm8 3v6c0 1.7-3.6 3-8 3s-8-1.3-8-3V6m16 6v6c0 1.7-3.6 3-8 3s-8-1.3-8-3v-6", keywords: "tables assets coverage warehouse" },
    ],
  },
  {
    // Where Satchel learns about the data (and its own settings); the pages above are what it knows.
    label: "Setup",
    dash: "bg-muted/40",
    items: [
      { to: "/connections", label: "Connections", icon: "M9 3v4M15 3v4M7 7h10v4a5 5 0 0 1-10 0zM12 16v5", keywords: "warehouse snowflake postgres bigquery duckdb database crawl key", min: "curator" },
      { to: "/sources", label: "Sources", icon: "M4 7c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zm0 0v5c0 1.7 3.6 3 8 3m-8 0v5c0 1.7 3.6 3 8 3m4-6h6m0 0-2.5-2.5M20 17l-2.5 2.5", keywords: "dbt sync import", min: "curator" },
      { to: "/settings", label: "Settings", icon: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm7.4-3a7.4 7.4 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7.5 7.5 0 0 0-2-1.2L14.5 3h-5l-.4 2.6a7.5 7.5 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1a7.5 7.5 0 0 0 2 1.2l.4 2.6h5l.4-2.6a7.5 7.5 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z", keywords: "configuration models llm api keys anthropic openai admin members users invite roles access limits deployment", min: "curator" },
    ],
  },
];

export const ALL_PAGES: NavItem[] = [DASHBOARD, ...NAV_GROUPS.flatMap((g) => g.items)];

/** Home for explorers and analysts: they ask questions in their agent and read context here. */
export const HOME: NavItem = { to: "/dashboard", label: "Home", icon: ICONS.dashboard, keywords: "home overview connect claude" };

/**
 * The sidebar for a role. Curators and admins get every group; everyone else gets Home and the
 * pages they can use (the context, the catalog, and questions), without the curation groups.
 */
export function navFor(role: string | null | undefined): { home: NavItem; groups: NavGroup[] } {
  if (atLeast(role, "curator")) return { home: DASHBOARD, groups: NAV_GROUPS };
  const page = (to: string) => ALL_PAGES.find((p) => p.to === to)!;
  return {
    home: HOME,
    groups: [{ label: "Explore", dash: "bg-good/55", items: [page("/library"), page("/catalog"), page("/sessions")] }],
  };
}

/** Every page and tab the role can open, for the command palette and the header breadcrumb. */
export function pagesFor(role: string | null | undefined): NavItem[] {
  const { home, groups } = navFor(role);
  return [home, ...groups.flatMap((g) => g.items), ...SUB_PAGES.filter((p) => !p.min || atLeast(role, p.min))];
}

/** Tabs worth finding by name in the command palette (each was its own page before). */
export const SUB_PAGES: NavItem[] = [
  { to: "/releases", label: "Changes", icon: "M6 3v12m0 0a3 3 0 1 0 0 6 3 3 0 0 0 0-6zm12-6a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm0 0c0 5-6 3-6 9", keywords: "releases preview publish unpublished drafts", min: "curator" },
  { to: "/releases?tab=history", label: "Release history", icon: ICONS.tag, keywords: "releases rollback versions published", min: "curator" },
  { to: "/sessions?tab=usage", label: "Usage", icon: "M4 20V10m6 10V4m6 16v-7m4 7H2", keywords: "observability sessions charts feedback tools served", min: "curator" },
  { to: "/settings?tab=jobs", label: "Jobs", icon: ICONS.clock, keywords: "settings queue background sync crawl curation retry worker", min: "curator" },
];

/** A page's sidebar icon, for its empty state. */
export function pageIcon(to: string): string | undefined {
  return ALL_PAGES.find((p) => p.to === to)?.icon;
}
