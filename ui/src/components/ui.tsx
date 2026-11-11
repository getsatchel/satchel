import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { errMsg } from "@/lib/format";
import { SatchelMark } from "@/components/Logo";
import { MOD_KEY, useCommandPalette } from "@/components/paletteContext";

/** True when the viewport matches `query`. Defaults to the `lg` breakpoint used for list + detail. */
export function useWide(query = "(min-width: 1024px)") {
  const [wide, setWide] = useState(() => (typeof window === "undefined" ? true : window.matchMedia(query).matches));
  useEffect(() => {
    const mq = window.matchMedia(query);
    const on = () => setWide(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [query]);
  return wide;
}

const SPLIT = {
  lg: { grid: "lg:grid-cols-[minmax(16rem,20rem)_minmax(0,1fr)]", show: "lg:block", hide: "lg:hidden" },
  xl: { grid: "xl:grid-cols-[minmax(0,1fr)_minmax(16rem,22rem)]", show: "xl:block", hide: "xl:hidden" },
} as const;

/** List and detail side by side from `split` up; below that, the list hides while a detail is open. */
export function ListDetail(props: {
  list: ReactNode;
  detail: ReactNode;
  hasDetail: boolean;
  onBack?: () => void;
  backLabel?: string;
  split?: keyof typeof SPLIT;
}) {
  const s = SPLIT[props.split ?? "lg"];
  return (
    <div className={`grid items-start gap-4 ${s.grid}`}>
      <div className={props.hasDetail ? `hidden ${s.show}` : ""}>{props.list}</div>
      {props.hasDetail ? (
        <div className="min-w-0">
          {props.onBack && (
            <button className={`btn btn-ghost btn-sm mb-2 -ml-2 ${s.hide}`} onClick={props.onBack}>
              <Icon d="M15 18l-6-6 6-6" size={14} />
              {props.backLabel ?? "Back"}
            </button>
          )}
          {props.detail}
        </div>
      ) : (
        <div className={`hidden min-w-0 ${s.show}`}>{props.detail}</div>
      )}
    </div>
  );
}

/** Card grid that fills the width with as many `min`-wide columns as fit. `fit` stretches a short row to the full width (stat rows). */
export function CardGrid({ min = "14rem", gap = "gap-3", fit, children }: { min?: string; gap?: string; fit?: boolean; children: ReactNode }) {
  return (
    <div className={`grid ${gap}`} style={{ gridTemplateColumns: `repeat(${fit ? "auto-fit" : "auto-fill"},minmax(min(${min},100%),1fr))` }}>
      {children}
    </div>
  );
}

/** Card wrapper for a data table. Put `<thead className="border-b border-line">` and a `divide-y` tbody inside. */
export function TableCard({ children, footer }: { children: ReactNode; footer?: ReactNode }) {
  return (
    <div className="card overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">{children}</table>
      </div>
      {footer}
    </div>
  );
}

/** A selectable row in a list that sits beside a detail pane. */
export function ListRow({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <li>
      <button
        onClick={onClick}
        aria-current={active}
        className={`block w-full px-3 py-2.5 text-left ${active ? "bg-accent-soft/70 shadow-[inset_2px_0_0_rgb(var(--accent))]" : "hover:bg-sunken/70"}`}
      >
        {children}
      </button>
    </li>
  );
}

/** "Library / folder/" line above a detail page title. */
export function Crumb({ to, label, rest }: { to: string; label: string; rest?: ReactNode }) {
  return (
    <p className="mb-2 break-all text-xs text-muted">
      <Link to={to} className="hover:text-ink">
        {label}
      </Link>
      {rest && (
        <>
          {" / "}
          {rest}
        </>
      )}
    </p>
  );
}

/** Item name on the first line, folder underneath — used in tables instead of truncating. */
export function KeyLines({ itemKey }: { itemKey: string }) {
  const i = itemKey.lastIndexOf("/");
  if (i < 0) return <span className="font-mono">{itemKey}</span>;
  return (
    <>
      <span className="font-mono [overflow-wrap:anywhere]">{itemKey.slice(i + 1)}</span>
      <span className="block font-mono text-xs text-muted [overflow-wrap:anywhere]">{itemKey.slice(0, i + 1)}</span>
    </>
  );
}

export function FilterSelect(props: { label: string; value: string; onChange: (v: string) => void; options: string[] }) {
  return (
    <select
      className={`input min-w-0 w-full sm:w-auto ${props.value ? "border-accent/60 bg-accent-soft/40" : "text-muted"}`}
      aria-label={props.label}
      value={props.value}
      onChange={(e) => props.onChange(e.target.value)}
    >
      <option value="">{props.label}: all</option>
      {props.options.map((o) => (
        <option key={o} value={o}>
          {props.label}: {o}
        </option>
      ))}
    </select>
  );
}

/** Page title and one-line description on the left; actions (buttons, ⌘K, "New") on the right. */
export function PageHeader(props: { title: string; sub?: ReactNode; actions?: ReactNode }) {
  const openPalette = useCommandPalette();
  return (
    <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="break-words text-xl font-semibold tracking-tight">{props.title}</h1>
        {props.sub && <div className="mt-1 max-w-3xl text-sm text-muted">{props.sub}</div>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className="btn btn-ghost hidden gap-1 font-mono text-xs sm:inline-flex" onClick={openPalette} aria-label="Open command palette" aria-keyshortcuts="Meta+K Control+K">
          <Icon d={ICONS.command} size={14} />
          {MOD_KEY === "⌘" ? "cmd+k" : "ctrl+k"}
        </button>
        {props.actions}
      </div>
    </div>
  );
}

export function EmptyState(props: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="card flex flex-col items-center px-6 py-12 text-center">
      <div className="mb-3 grid h-10 w-10 place-items-center rounded-full bg-accent-soft text-accent" aria-hidden>
        <SatchelMark size={20} />
      </div>
      <h2 className="text-base font-medium">{props.title}</h2>
      {props.children && <p className="mt-1 max-w-md text-sm text-muted">{props.children}</p>}
      {props.action && <div className="mt-4">{props.action}</div>}
    </div>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" className="flex items-center gap-2 py-8 text-sm text-muted">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-line border-t-accent" />
      {label}...
    </div>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  return (
    <div role="alert" className="rounded-md border border-bad/30 bg-bad-soft px-3 py-2 text-sm text-bad">
      {errMsg(error)}
    </div>
  );
}

export function Notice(props: { tone?: "info" | "warn"; children: ReactNode }) {
  const cls =
    props.tone === "warn"
      ? "border-warn/30 bg-warn-soft text-warn"
      : "border-info/25 bg-info-soft text-ink";
  return <div className={`rounded-md border px-3 py-2 text-sm ${cls}`}>{props.children}</div>;
}

export function TypeBadge({ type }: { type: string }) {
  return <span className="badge font-mono">{type}</span>;
}

const STATE_TONE: Record<string, Tone> = { published: "good", draft: "warn", proposed: "accent", deprecated: "muted" };

export function StateBadge({ state }: { state: string }) {
  return <StatusDot tone={STATE_TONE[state] ?? "muted"}>{state}</StatusDot>;
}

export function GitBadge() {
  return (
    <span className="badge badge-accent" title="Managed in the context git repository">
      git
    </span>
  );
}

/** Flag for items synced from an upstream tool ("From dbt: model.jaffle_shop.orders"). */
export function SourceBadge({ kind, sourceKey }: { kind?: string | null; sourceKey?: string | null }) {
  return (
    <span className="badge badge-accent max-w-full" title={`Synced from ${kind ?? "a source"}${sourceKey ? `: ${sourceKey}` : ""}. The source block is read-only.`}>
      <span className="shrink-0">From {kind ?? "source"}</span>
      {sourceKey && <span className="min-w-0 truncate font-mono font-normal opacity-80">{sourceKey}</span>}
    </span>
  );
}

export function ItemLink({ itemKey, seq }: { itemKey: string; seq?: number | null }) {
  return (
    <Link to={`/library/item/${itemKey.split("/").map(encodeURIComponent).join("/")}`} className="font-mono text-accent [overflow-wrap:anywhere] hover:underline">
      <ItemKey itemKey={itemKey} />
      {seq != null && <span className="text-muted">@v{seq}</span>}
    </Link>
  );
}

/** An item key with its folder dimmed so the name reads first. `fit` truncates the folder before the name. */
export function ItemKey({ itemKey, fit }: { itemKey: string; fit?: boolean }) {
  const i = itemKey.lastIndexOf("/");
  if (i < 0) return fit ? <span className="truncate">{itemKey}</span> : <>{itemKey}</>;
  const dir = itemKey.slice(0, i + 1);
  const name = itemKey.slice(i + 1);
  if (fit) {
    return (
      <span className="flex min-w-0" title={itemKey}>
        <span className="min-w-[2ch] shrink-[1000] truncate opacity-60">{dir}</span>
        <span className="min-w-0 truncate">{name}</span>
      </span>
    );
  }
  return (
    <>
      <span className="opacity-60">{dir}</span>
      {name}
    </>
  );
}

export function Modal(props: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (props.open && !d.open) d.showModal();
    if (!props.open && d.open) d.close();
  }, [props.open]);
  return (
    <dialog
      ref={ref}
      onClose={props.onClose}
      onClick={(e) => {
        if (e.target === ref.current) props.onClose();
      }}
      className={`w-[calc(100%-1.5rem)] rounded-xl border border-line bg-surface p-0 text-ink shadow-pop backdrop:bg-black/40 ${props.wide ? "max-w-4xl" : "max-w-md"}`}
    >
      {props.open && (
        <div className="flex max-h-[85vh] flex-col">
          <div className="border-b border-line px-5 py-4 text-base font-semibold">{props.title}</div>
          <div className="overflow-auto px-5 py-5">{props.children}</div>
          {props.footer && <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line px-5 py-4">{props.footer}</div>}
        </div>
      )}
    </dialog>
  );
}

export function Tabs<T extends string>(props: {
  tabs: { id: T; label: string }[];
  value: T;
  onChange: (id: T) => void;
}) {
  return (
    <div role="tablist" className="mb-3 flex gap-1 overflow-x-auto border-b border-line">
      {props.tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={props.value === t.id}
          onClick={() => props.onChange(t.id)}
          className={`-mb-px shrink-0 whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium ${
            props.value === t.id ? "border-accent text-ink" : "border-transparent text-muted hover:text-ink"
          }`}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Segmented<T extends string | number>(props: {
  options: { id: T; label: string }[];
  value: T;
  onChange: (id: T) => void;
  label: string;
}) {
  return (
    <div role="group" aria-label={props.label} className="inline-flex max-w-full overflow-x-auto rounded-md border border-line bg-surface p-0.5">
      {props.options.map((o) => (
        <button
          key={String(o.id)}
          type="button"
          aria-pressed={props.value === o.id}
          onClick={() => props.onChange(o.id)}
          className={`h-8 shrink-0 whitespace-nowrap rounded px-2.5 text-xs font-medium sm:h-7 ${
            props.value === o.id ? "bg-primary text-primary-ink" : "text-muted hover:text-ink"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export type Tone = "good" | "warn" | "bad" | "info" | "accent" | "muted";

const DOT: Record<Tone, string> = { good: "bg-good", warn: "bg-warn", bad: "bg-bad", info: "bg-info", accent: "bg-accent", muted: "bg-muted/50" };
const TEXT: Record<Tone, string> = { good: "text-good", warn: "text-warn", bad: "text-bad", info: "text-info", accent: "text-accent", muted: "text-muted" };

export const ICONS = {
  chevron: "m9 6 6 6-6 6",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zm9 2-3.5-3.5",
  close: "M6 6l12 12M18 6 6 18",
  check: "m5 12 5 5 9-10",
  thumbUp: "M7 10v10H4V10zm0 0 4-7a2 2 0 0 1 2 2v4h5.5a2 2 0 0 1 2 2.3l-1.2 7A2 2 0 0 1 17.3 20H7",
  thumbDown: "M7 14V4H4v10zm0 0 4 7a2 2 0 0 0 2-2v-4h5.5a2 2 0 0 0 2-2.3l-1.2-7A2 2 0 0 0 17.3 4H7",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zm0-5v-5m0-3h.01",
  plus: "M12 5v14M5 12h14",
  down: "m6 9 6 6 6-6",
  upDown: "m8 9 4-4 4 4M8 15l4 4 4-4",
  external: "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
  command: "M9 6v12M15 6v12M6 9h12M6 15h12M9 6a3 3 0 1 0-3 3M15 6a3 3 0 1 1 3 3M9 18a3 3 0 1 1-3-3M15 18a3 3 0 1 0 3-3",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2m0 18v2M4.2 4.2l1.4 1.4m12.8 12.8 1.4 1.4M1 12h2m18 0h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  logout: "M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 17l5-5-5-5M15 12H3",
  play: "M7 4v16l13-8z",
  pulse: "M3 12h4l3-8 4 16 3-8h4",
  inbox: "M3 13h5l1 3h6l1-3h5M5 5h14l2 8v6H3v-6z",
  heart: "M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z",
  dashboard: "M4 13a8 8 0 1 1 16 0M12 13l4-4M7 17h10",
  book: "M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2zM4 19a2 2 0 0 1 2-2h13",
  list: "M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01",
  edit: "M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4",
  tag: "M3 12V4h8l10 10-8 8zM7.5 8.5h.01",
  database: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  flask: "M9 3h6M10 3v6l-5.5 9.5A1.5 1.5 0 0 0 5.8 21h12.4a1.5 1.5 0 0 0 1.3-2.5L14 9V3M7.5 15h9",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
  alert: "M12 3 2 20h20zM12 10v4m0 3h.01",
  link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3A4 4 0 0 0 11 18.7l1-1",
  layers: "m12 3 9 5-9 5-9-5zM3 13l9 5 9-5M3 17l9 5 9-5",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
  bolt: "M13 2 4 14h7l-1 8 9-12h-7z",
  git: "M6 3v12m0 0a3 3 0 1 0 0 6 3 3 0 0 0 0-6zm12-6a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm0 0c0 4-6 3-12 6",
  doc: "M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h6",
};

export function Icon({ d, size = 15, className = "", strokeWidth = 1.7 }: { d: string; size?: number; className?: string; strokeWidth?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" className={`shrink-0 ${className}`} aria-hidden>
      <path d={d} />
    </svg>
  );
}

export function Chevron({ open }: { open: boolean }) {
  return <Icon d={ICONS.chevron} size={12} strokeWidth={2.4} className={`text-muted transition-transform ${open ? "rotate-90" : ""}`} />;
}

/** A health signal: coloured dot plus a caps mono state word. Use for states, not for flags. */
export function StatusDot({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap font-mono text-2xs font-medium uppercase tracking-[0.06em] ${TEXT[tone]}`}>
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${DOT[tone]}`} aria-hidden />
      {children}
    </span>
  );
}

/** Caps mono label (Geist Mono, 11px, uppercase, tracked). */
export function Label({ children, className = "", as: As = "span" }: { children: ReactNode; className?: string; as?: "span" | "div" | "h2" | "h3" | "dt" }) {
  return <As className={`label-caps ${className}`}>{children}</As>;
}

/** Section heading: optional icon, caps label and count, a hairline rule filling the row, then actions. */
export function SectionHeading(props: { title: ReactNode; count?: number; actions?: ReactNode; id?: string; icon?: string }) {
  return (
    <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2">
      <h2 id={props.id} className="label-caps flex min-w-0 items-center gap-2">
        {props.icon && <Icon d={props.icon} size={14} className="text-muted" />}
        <span className="min-w-0">{props.title}</span>
        {props.count != null && <span className="tabular-nums text-muted/70">{props.count}</span>}
      </h2>
      <span className="h-px min-w-6 flex-1 bg-line" aria-hidden />
      {props.actions && <div className="flex flex-wrap items-center gap-2">{props.actions}</div>}
    </div>
  );
}

/** "● SENDING · 2h ago": a state dot, caps state word and a muted timestamp. */
export function StatusLine({ tone, state, time, title }: { tone: Tone; state: ReactNode; time?: ReactNode; title?: string }) {
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5 whitespace-nowrap" title={title}>
      <span className={`h-2 w-2 shrink-0 rounded-full ${DOT[tone]}`} aria-hidden />
      <span className="font-mono text-2xs font-semibold uppercase tracking-[0.08em] text-ink">{state}</span>
      {time != null && (
        <>
          <span className="text-muted/60" aria-hidden>
            ·
          </span>
          <span className="truncate text-xs text-muted">{time}</span>
        </>
      )}
    </span>
  );
}

/** Keyboard key cap. */
export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

/** Outline caps pill with a trailing chevron ("VIEW SESSIONS >"). Renders a Link when `to` is set. */
export function PillButton(props: { to?: string; onClick?: () => void; children: ReactNode; chevron?: boolean }) {
  const inner = (
    <>
      {props.children}
      {props.chevron !== false && <Icon d={ICONS.chevron} size={12} strokeWidth={2.2} />}
    </>
  );
  return props.to ? (
    <Link to={props.to} className="btn-pill">
      {inner}
    </Link>
  ) : (
    <button type="button" className="btn-pill" onClick={props.onClick}>
      {inner}
    </button>
  );
}

/** Small (i) with a tooltip on hover and focus. Positioned `fixed` so card overflow never clips it. */
export function InfoTip({ text, label = "More info" }: { text: string; label?: string }) {
  const ref = useRef<HTMLButtonElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const show = () => {
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    const w = Math.min(240, window.innerWidth - 16);
    setPos({ top: r.bottom + 6, left: Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8)) });
  };
  const id = useId();
  return (
    <>
      <button
        ref={ref}
        type="button"
        aria-label={label}
        aria-describedby={pos ? id : undefined}
        onMouseEnter={show}
        onMouseLeave={() => setPos(null)}
        onFocus={show}
        onBlur={() => setPos(null)}
        className="-m-1.5 grid h-7 w-7 shrink-0 place-items-center rounded-full text-muted/70 hover:text-ink"
      >
        <Icon d={ICONS.info} size={14} />
      </button>
      {pos && (
        <span
          id={id}
          role="tooltip"
          className="pointer-events-none fixed z-50 w-[min(15rem,calc(100vw-1rem))] rounded-md border border-line bg-surface px-2.5 py-1.5 text-xs text-ink shadow-pop"
          style={{ top: pos.top, left: pos.left }}
        >
          {text}
        </span>
      )}
    </>
  );
}

export type Glyph = "dot" | "ring" | "square" | "triangle" | "diamond";

/** Small coloured shape that marks a stat or status. Colour only lives in glyphs and dots. */
export function StatGlyph({ tone, shape = "dot" }: { tone: Tone; shape?: Glyph }) {
  const c = TEXT[tone];
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" className={`shrink-0 ${c}`} aria-hidden>
      {shape === "dot" && <circle cx="6" cy="6" r="5" fill="currentColor" />}
      {shape === "ring" && <circle cx="6" cy="6" r="4.2" fill="none" stroke="currentColor" strokeWidth="1.6" />}
      {shape === "square" && <rect x="1.5" y="1.5" width="9" height="9" rx="2" fill="currentColor" />}
      {shape === "triangle" && <path d="M6 1.2 11 10.5H1z" fill="currentColor" />}
      {shape === "diamond" && <path d="M6 .8 11.2 6 6 11.2.8 6z" fill="currentColor" />}
    </svg>
  );
}

/** Grid of stat cells divided by hairlines. `cols` is the column count from `sm` up (two columns on phones). */
export function StatGrid({ cols = 4, children }: { cols?: 2 | 3 | 4; children: ReactNode }) {
  const c = { 2: "sm:grid-cols-2", 3: "sm:grid-cols-3", 4: "sm:grid-cols-2 lg:grid-cols-4" }[cols];
  return <div className={`grid grid-cols-2 gap-px bg-line ${c}`}>{children}</div>;
}

/** One cell in a StatGrid: glyph, caps label, optional info tooltip, big value and a muted secondary value. */
export function StatCell(props: { label: string; value: ReactNode; secondary?: ReactNode; tone?: Tone; glyph?: Glyph; info?: string; to?: string }) {
  const body = (
    <>
      <div className="flex min-w-0 items-center gap-2">
        <StatGlyph tone={props.tone ?? "muted"} shape={props.glyph} />
        <span className="label-caps min-w-0 font-semibold leading-tight text-ink/80 [overflow-wrap:anywhere]">{props.label}</span>
        {props.info && (
          <span className="ml-auto">
            <InfoTip text={props.info} label={`About ${props.label}`} />
          </span>
        )}
      </div>
      <div className="mt-2 flex min-w-0 flex-wrap items-baseline gap-x-2">
        <span className="text-lg font-semibold tabular-nums tracking-tight">{props.value}</span>
        {props.secondary != null && <span className="min-w-0 truncate font-mono text-xs tabular-nums text-muted">{props.secondary}</span>}
      </div>
    </>
  );
  return <div className="min-w-0 bg-surface px-4 py-3.5 sm:px-5">{body}</div>;
}

/** Card section header with a sentence-case lead and actions on the right. */
export function CardHeader({ children, actions }: { children: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-3 sm:px-5">
      <div className="min-w-0 text-sm text-ink/80">{children}</div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

/** Centred footer for pill buttons at the bottom of a card. */
export function CardFooter({ children }: { children: ReactNode }) {
  return <div className="flex flex-wrap justify-center gap-2 border-t border-line px-4 py-3">{children}</div>;
}

export interface MenuItem {
  label: string;
  icon?: string;
  hint?: ReactNode;
  to?: string;
  href?: string;
  onSelect?: () => void;
  disabled?: boolean;
}

/** Button that opens a menu. Arrow keys move, Enter selects, Esc closes. `up` opens above the trigger. */
export function Dropdown(props: {
  trigger: ReactNode;
  label: string;
  items: MenuItem[];
  header?: ReactNode;
  align?: "left" | "right";
  up?: boolean;
  className?: string;
  menuClassName?: string;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const itemsRef = useRef<(HTMLElement | null)[]>([]);
  const nav = useNavigate();
  const id = useId();
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    root.current?.querySelector<HTMLElement>('[role="menuitem"]:not(:disabled)')?.focus();
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);
  const close = () => {
    setOpen(false);
    root.current?.querySelector<HTMLButtonElement>("button[aria-haspopup]")?.focus();
  };
  const choose = (it: MenuItem) => {
    if (it.disabled) return;
    setOpen(false);
    if (it.to) nav(it.to);
    else if (it.href) window.open(it.href, "_blank", "noopener");
    it.onSelect?.();
  };
  const onKey = (e: KeyboardEvent) => {
    const els = itemsRef.current.filter((x): x is HTMLElement => Boolean(x));
    const i = els.indexOf(document.activeElement as HTMLElement);
    if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      els[(i + 1) % els.length]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      els[(i - 1 + els.length) % els.length]?.focus();
    } else if (e.key === "Tab") setOpen(false);
  };
  return (
    <div ref={root} className={`relative ${props.className ?? ""}`} onKeyDown={open ? onKey : undefined}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-label={props.label}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (!open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
            e.preventDefault();
            setOpen(true);
          }
        }}
        className={props.menuClassName ?? "btn"}
      >
        {props.trigger}
      </button>
      {open && (
        <div
          id={id}
          role="menu"
          aria-label={props.label}
          className={`absolute z-40 min-w-[13rem] overflow-hidden rounded-lg border border-line bg-surface py-1 shadow-pop ${
            props.align === "left" ? "left-0" : "right-0"
          } ${props.up ? "bottom-full mb-1.5" : "top-full mt-1.5"}`}
        >
          {props.header && <div className="border-b border-line px-3 py-2">{props.header}</div>}
          {props.items.map((it, n) => (
            <button
              key={it.label}
              ref={(el) => {
                itemsRef.current[n] = el;
              }}
              type="button"
              role="menuitem"
              disabled={it.disabled}
              onClick={() => choose(it)}
              className="flex h-10 w-full items-center gap-2.5 px-3 text-left text-sm text-ink hover:bg-sunken focus:bg-sunken focus:outline-none disabled:opacity-50 sm:h-9"
            >
              {it.icon && <Icon d={it.icon} size={15} className="text-muted" />}
              <span className="min-w-0 flex-1 truncate">{it.label}</span>
              {it.hint && <span className="text-xs text-muted">{it.hint}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function SearchInput(props: { value: string; onChange: (v: string) => void; placeholder: string; label: string; className?: string }) {
  return (
    <label className={`relative block ${props.className ?? "w-full sm:w-64"}`}>
      <Icon d={ICONS.search} size={14} strokeWidth={2} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted" />
      <input className="input w-full pl-8" type="search" placeholder={props.placeholder} aria-label={props.label} value={props.value} onChange={(e) => props.onChange(e.target.value)} />
    </label>
  );
}

/** Key/value metadata. Pass pairs; null values are skipped. */
export function DescList({ rows, labelWidth = "7rem" }: { rows: [string, ReactNode][]; labelWidth?: string }) {
  return (
    <dl
      className="grid grid-cols-1 gap-y-2 text-sm sm:grid-cols-[minmax(5rem,var(--dl-label))_minmax(0,1fr)] sm:gap-x-3 sm:gap-y-1.5"
      style={{ ["--dl-label" as string]: labelWidth }}
    >
      {rows
        .filter(([, v]) => v != null && v !== false)
        .map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-xs text-muted sm:text-sm">{k}</dt>
            <dd className="min-w-0">{v}</dd>
          </div>
        ))}
    </dl>
  );
}

export function PanelSection({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <div className="border-t border-line px-4 py-3 first:border-t-0">
      <h3 className="label-caps mb-2">{title}</h3>
      {children}
    </div>
  );
}

export function Feedback({ up, down }: { up: number; down: number }) {
  return (
    <span className="inline-flex items-center gap-2.5 whitespace-nowrap tabular-nums">
      <span className={`inline-flex items-center gap-1 ${up ? "text-good" : "text-muted"}`} aria-label={`${up} positive`}>
        <Icon d={ICONS.thumbUp} size={13} />
        {up}
      </span>
      <span className={`inline-flex items-center gap-1 ${down ? "text-bad" : "text-muted"}`} aria-label={`${down} negative`}>
        <Icon d={ICONS.thumbDown} size={13} />
        {down}
      </span>
    </span>
  );
}

export function Meter({ value, label, tone = "accent" }: { value: number; label: string; tone?: Tone }) {
  const pct = Math.max(0, Math.min(100, Math.round(value)));
  return (
    <div className="h-1.5 overflow-hidden rounded-full bg-sunken" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
      <div className={`h-full rounded-full ${DOT[tone]}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

export function Stat(props: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="card px-4 py-3">
      <div className="label-caps">{props.label}</div>
      <div className="mt-0.5 text-xl font-semibold tabular-nums">{props.value}</div>
      {props.sub && <div className="text-xs text-muted">{props.sub}</div>}
    </div>
  );
}

export interface DocRef {
  source: string;
  key: string;
  primary: boolean;
}

/** Other docs describing the same table: "dbt, cube", each linking to its item. Renders nothing when empty. */
export function DescribedBy({ refs, label = "Also described by" }: { refs: DocRef[] | undefined; label?: string }) {
  if (!refs?.length) return null;
  return (
    <p className="text-xs text-muted">
      {label}:{" "}
      {refs.map((r, i) => (
        <span key={r.key}>
          {i > 0 && ", "}
          <Link to={`/library/item/${r.key.split("/").map(encodeURIComponent).join("/")}`} className="text-accent hover:underline" title={r.key}>
            {r.source}
          </Link>
        </span>
      ))}
    </p>
  );
}
