import { ICONS } from "@/components/ui";

/** Where the sidebar's "Docs" link points. */
export const DOCS_URL = "https://github.com/getsatchel/satchel/tree/main/docs";

export interface NavItem {
  to: string;
  label: string;
  icon: string;
  /** Extra words the command palette matches on. */
  keywords?: string;
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
      { to: "/inbox", label: "Inbox", icon: ICONS.inbox, keywords: "suggestions proposals review" },
      { to: "/library", label: "Library", icon: "M4 4h6v16H4zM12 4h3v16h-3zM17 6l3-1 2 15-3 1z", keywords: "items context docs glossary" },
      { to: "/changes", label: "Changes", icon: "M6 3v12m0 0a3 3 0 1 0 0 6 3 3 0 0 0 0-6zm12-6a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm0 0c0 5-6 3-6 9", keywords: "preview publish unpublished drafts" },
      { to: "/releases", label: "Releases", icon: "M12 3l9 5v8l-9 5-9-5V8zM12 12l9-4M12 12v9M12 12L3 8", keywords: "rollback versions" },
    ],
  },
  {
    label: "Observe",
    dash: "bg-info/55",
    items: [
      { to: "/sessions", label: "Sessions", icon: "M4 6h16M4 12h10M4 18h13", keywords: "agents runs replay" },
      { to: "/observability", label: "Observability", icon: "M4 20V10m6 10V4m6 16v-7m4 7H2", keywords: "usage charts feedback" },
      { to: "/evals", label: "Evals", icon: "M9 11l3 3 8-8M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9", keywords: "suites runs tests" },
    ],
  },
  {
    label: "Data",
    dash: "bg-good/55",
    items: [
      { to: "/catalog", label: "Catalog", icon: "M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3zm8 3v6c0 1.7-3.6 3-8 3s-8-1.3-8-3V6m16 6v6c0 1.7-3.6 3-8 3s-8-1.3-8-3v-6", keywords: "tables assets coverage warehouse" },
      { to: "/sources", label: "Sources", icon: "M4 7c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zm0 0v5c0 1.7 3.6 3 8 3m-8 0v5c0 1.7 3.6 3 8 3m4-6h6m0 0-2.5-2.5M20 17l-2.5 2.5", keywords: "dbt sync import" },
    ],
  },
  {
    label: "Admin",
    dash: "bg-muted/40",
    items: [
      { to: "/settings", label: "Settings", icon: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm7.4-3a7.4 7.4 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7.5 7.5 0 0 0-2-1.2L14.5 3h-5l-.4 2.6a7.5 7.5 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1a7.5 7.5 0 0 0 2 1.2l.4 2.6h5l.4-2.6a7.5 7.5 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z", keywords: "configuration models llm api keys anthropic openai admin" },
    ],
  },
];

export const ALL_PAGES: NavItem[] = [DASHBOARD, ...NAV_GROUPS.flatMap((g) => g.items)];
