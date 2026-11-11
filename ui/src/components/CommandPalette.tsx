import { useCallback, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import { useToast } from "@/components/Toast";
import { Dropdown, Icon, ICONS, Kbd, type MenuItem } from "@/components/ui";
import { ALL_PAGES } from "@/components/nav";
import { errMsg } from "@/lib/format";
import { toggleTheme, useTheme } from "@/lib/theme";

import { PaletteCtx, useCommandPalette, MOD_KEY } from "@/components/paletteContext";

export { useCommandPalette, MOD_KEY };
const Ctx = PaletteCtx;

/** Runs curation now (admin only on the server too). Shared by the palette and the "New" menu. */
function useRunCuration() {
  const qc = useQueryClient();
  const toast = useToast();
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me, staleTime: 60_000 });
  const run = useMutation({
    mutationFn: () => endpoints.runCuration(true),
    onSuccess: (r) => {
      toast(
        "ok",
        `Curation finished: ${r.suggestions_created} new, ${r.suggestions_extended} extended, ${r.drafted} drafted` +
          (r.llm === "none" ? " (no LLM configured, drafting skipped)" : ""),
      );
      if (r.errors.length) toast("err", r.errors.join("; "));
      void qc.invalidateQueries({ queryKey: ["suggestions"] });
      void qc.invalidateQueries({ queryKey: ["items"] });
      void qc.invalidateQueries({ queryKey: ["changes"] });
    },
    onError: (e) => toast("err", errMsg(e)),
  });
  return { allowed: me.data?.role === "admin", run };
}

interface Command {
  id: string;
  group: "Pages" | "Actions";
  label: string;
  icon: string;
  keywords?: string;
  run: () => void;
}

export function CommandPaletteProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const show = useCallback(() => setOpen(true), []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return (
    <Ctx.Provider value={show}>
      {children}
      {open && <Palette onClose={() => setOpen(false)} />}
    </Ctx.Provider>
  );
}

function Palette({ onClose }: { onClose: () => void }) {
  const nav = useNavigate();
  const [theme] = useTheme();
  const curation = useRunCuration();
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const restore = useRef<Element | null>(null);
  const id = useId();

  useEffect(() => {
    restore.current = document.activeElement;
    input.current?.focus();
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
      if (restore.current instanceof HTMLElement) restore.current.focus();
    };
  }, []);

  const commands = useMemo<Command[]>(() => {
    const go = (to: string) => () => nav(to);
    const cmds: Command[] = ALL_PAGES.map((p) => ({ id: p.to, group: "Pages", label: p.label, icon: p.icon, keywords: p.keywords, run: go(p.to) }));
    cmds.push({ id: "new-item", group: "Actions", label: "New context item", icon: ICONS.plus, keywords: "create add guide glossary", run: go("/library/new") });
    cmds.push({ id: "publish", group: "Actions", label: "Preview and publish changes", icon: ICONS.play, keywords: "release ship", run: go("/changes") });
    if (curation.allowed) {
      cmds.push({ id: "curate", group: "Actions", label: "Run curation now", icon: ICONS.pulse, keywords: "suggestions signals", run: () => curation.run.mutate() });
    }
    cmds.push({ id: "theme", group: "Actions", label: theme === "dark" ? "Switch to light theme" : "Switch to dark theme", icon: theme === "dark" ? ICONS.sun : ICONS.moon, keywords: "theme dark light mode", run: toggleTheme });
    return cmds;
  }, [nav, theme, curation.allowed, curation.run]);

  const shown = useMemo(() => {
    const t = q.trim().toLowerCase();
    if (!t) return commands;
    return commands.filter((c) => `${c.label} ${c.keywords ?? ""} ${c.group}`.toLowerCase().includes(t));
  }, [commands, q]);

  useEffect(() => setActive(0), [q]);
  useEffect(() => {
    list.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const choose = (c: Command | undefined) => {
    if (!c) return;
    onClose();
    c.run();
  };

  const onKey = (e: ReactKeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => (shown.length ? (a + 1) % shown.length : 0));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => (shown.length ? (a - 1 + shown.length) % shown.length : 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      choose(shown[active]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    } else if (e.key === "Tab") {
      e.preventDefault();
    }
  };

  let lastGroup = "";
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/30 px-3 pt-[12vh] backdrop-blur-[2px]" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        className="w-full max-w-lg overflow-hidden rounded-xl border border-line bg-surface shadow-pop"
        onMouseDown={(e) => e.stopPropagation()}
        onKeyDown={onKey}
      >
        <div className="flex items-center gap-2.5 border-b border-line px-4">
          <Icon d={ICONS.search} size={16} className="text-muted" />
          <input
            ref={input}
            role="combobox"
            aria-expanded="true"
            aria-controls={`${id}-list`}
            aria-activedescendant={shown[active] ? `${id}-${shown[active].id}` : undefined}
            aria-autocomplete="list"
            className="h-12 min-w-0 flex-1 bg-transparent text-base outline-none placeholder:text-muted/70 focus-visible:outline-none"
            placeholder="Jump to a page or run an action"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <Kbd>Esc</Kbd>
        </div>
        <ul ref={list} id={`${id}-list`} role="listbox" aria-label="Commands" className="max-h-[min(60vh,24rem)] overflow-y-auto py-1.5">
          {shown.length === 0 && <li className="px-4 py-6 text-center text-sm text-muted">Nothing matches “{q.trim()}”.</li>}
          {shown.map((c, i) => {
            const head = c.group !== lastGroup;
            lastGroup = c.group;
            return (
              <li key={c.id} role="presentation">
                {head && <div className="label-caps px-4 pb-1 pt-2.5">{c.group}</div>}
                <div
                  id={`${id}-${c.id}`}
                  role="option"
                  aria-selected={i === active}
                  data-index={i}
                  onMouseMove={() => setActive(i)}
                  onClick={() => choose(c)}
                  className={`mx-1.5 flex h-10 cursor-pointer items-center gap-3 rounded-md px-2.5 text-sm ${
                    i === active ? "bg-sunken text-ink shadow-[inset_2px_0_0_rgb(var(--accent))]" : "text-ink/85"
                  }`}
                >
                  <Icon d={c.icon} size={16} className="text-muted" />
                  <span className="flex-1 truncate">{c.label}</span>
                  {i === active && <Icon d="M9 10l-4 4 4 4M5 14h11a3 3 0 0 0 3-3V5" size={14} className="text-muted" />}
                </div>
              </li>
            );
          })}
        </ul>
        <div className="flex items-center gap-3 border-t border-line px-4 py-2 text-xs text-muted">
          <span className="inline-flex items-center gap-1">
            <Kbd>↑</Kbd>
            <Kbd>↓</Kbd> move
          </span>
          <span className="inline-flex items-center gap-1">
            <Kbd>↵</Kbd> open
          </span>
        </div>
      </div>
    </div>
  );
}

/** The ink "New" menu. The ⌘K hint button is rendered by `PageHeader` itself. */
export function HeaderActions() {
  const curation = useRunCuration();
  const items: MenuItem[] = [
    { label: "Context item", icon: ICONS.plus, to: "/library/new" },
    { label: "Release preview", icon: ICONS.play, to: "/changes" },
  ];
  if (curation.allowed) items.push({ label: "Curation run", icon: ICONS.pulse, onSelect: () => curation.run.mutate(), disabled: curation.run.isPending });
  return (
    <>
      <Dropdown
        label="New"
        items={items}
        menuClassName="btn btn-primary font-mono text-2xs uppercase tracking-[0.08em]"
        trigger={
          <>
            New
            <Icon d={ICONS.down} size={13} strokeWidth={2.2} />
          </>
        }
      />
    </>
  );
}
