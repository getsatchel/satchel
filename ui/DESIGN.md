# Satchel UI design standard

The curator UI is a read-heavy tool: people scan lists, tables and diffs to decide what to change. The look is **ink on paper**: near-black text on off-white, neutral greys, one cobalt accent, colour only in small glyphs and dots. Every rule below serves one goal: **the thing you need to act on is the first thing you read.**

Who the UI is for and what earns space on a page (summary first, depth on demand; one home per fact; show the exception, not the default) are in [`PRODUCT.md`](PRODUCT.md). Read it before adding to a page.

Tokens live in `src/index.css` (CSS variables, light and dark, switched by `data-theme` on `<html>` via `src/lib/theme.ts`) and `tailwind.config.ts`. Shared components live in `src/components/ui.tsx`. Use them before writing new markup.

## Tokens

| Token | Light | Dark | Use for |
| --- | --- | --- | --- |
| `backdrop` | `#EAEAE7` | `#08090A` | the flat ground behind the app panel (md+ only), one step darker than `bg` |
| `bg` | `#F6F6F4` | `#0F1012` | the panel, sidebar and page background |
| `surface` | `#FCFCFB` | `#16171A` | cards, menus, dialogs, inputs |
| `sunken` | `#EFEFEC` | `#1D1E22` | hover, table header tint, skeletons, code |
| `line` | `#E2E2DE` | `#26282D` | every border and hairline |
| `ink` / `muted` | `#0F1011` / `#68696C` | `#EEEEEB` / `#96989D` | body text / secondary text and labels |
| `primary` / `primary-ink` | ink / bg | near-white / dark | primary action (`.btn-primary`, selected segment) |
| `accent` | cobalt `#2F45E8` | `#8796FF` | logo mark, links, selection bar, focus ring, endorsed |
| `good` `warn` `bad` `info` | green, amber, red, indigo | lighter variants | state glyphs and dots; each has a `-soft` background |

- **Neutrals are neutral.** No cream, sand or brown tint: the greys sit a hair warm of pure grey so they never read cold, and nothing else in the palette carries warmth.
- **Primary actions are ink, not accent.** Accent marks *where you are* (links, the active nav bar, focus); ink marks *what to do*.
- Tones carry meaning, never decoration: `good` healthy/live, `warn` needs a human (drafts, unpublished, pending), `bad` broken/excluded, `info` agent-originated or neutral activity. "Needs attention" is always `warn`.
- Colour lives in glyphs, dots, small badges and the selection bar. Never fill a card or a header with a tone.

## Type

- **Geist** for UI text, **Geist Mono** for identifiers and labels. Both are bundled from npm (`@fontsource-variable/*`); never load fonts from a CDN (the UI ships in the wheel and may run air-gapped).
- Scale: `text-2xs` 11px (caps labels only), `text-xs` 12px metadata, `text-sm` 13px body and tables, `text-base` 14px, `text-lg` stat values and empty-state headings.
- **Page titles** (`PageHeader`) are the one large, light line on a page: 22px (26px from `sm`), `font-light`, `tracking-[-0.03em]`. Everything under them keeps the dense scale above.
- **Caps label rule.** `.label-caps` / `Label` (Geist Mono, 11px, uppercase, `tracking-[0.08em]`, muted) is used for exactly these: section headings, sidebar group labels, stat labels, table column headers (`.th`), panel sub-headings, status words (`StatusDot`, `StatusLine`), pill buttons. Everything else, including body text, descriptions, buttons and menu items, is sentence-case sans.
- Identifiers (item keys, table and column names, SQL types, versions) are `font-mono`. Numbers that line up get `tabular-nums`.

## Layout

- **Frame.** From `md` the app is one rounded panel (`rounded-2xl`, hairline border, `shadow-panel`) floating on the flat `backdrop` with a 12–16px margin. No gradients. Below `md` it is full-bleed with a top bar and a slide-in drawer.
- **Sidebar.** Wordmark, workspace row, Search (`⌘K`), Dashboard, then groups. Each group has a short coloured dash and a caps label. Bottom: Docs ↗ and the account row (menu: theme, sign out).
- **Page.** `PageHeader` (title, one-line description, actions right), then sections separated by `space-y-9`. Each section starts with `SectionHeading` (icon, caps label, hairline rule).
- **Cards.** `.card` (`rounded-xl`, hairline, `bg-surface`, no shadow). Stats inside a card go in a `StatGrid` (hairline-divided cells). Navigation out of a card is a centred `PillButton` in `CardFooter`.
- **List + detail** (Catalog, Inbox, Suggestions): `ListDetail`. Side by side from `lg`/`xl`; below that one pane with a Back control. Don't auto-select on narrow screens.
- No fixed column grids without a breakpoint. `CardGrid` for card grids; `StatGrid` is two columns on phones. No horizontal page scroll at 390px.
- Tables: primary column `w-full`, secondary columns `w-px whitespace-nowrap`, hide less important columns at smaller breakpoints. `TableCard` scrolls instead of clipping.

## Sidebar IA

| Group | Pages |
| --- | --- |
| (top) | Dashboard (`/dashboard`, the default route) |
| CURATE | Inbox, Library, Changes, Releases |
| OBSERVE | Sessions, Observability, Evals, Jobs |
| DATA | Catalog, Sources |
| (bottom) | Docs ↗ (`DOCS_URL` in `src/components/nav.ts`), account menu |

Add a page by adding it to `NAV_GROUPS` in `src/components/nav.ts`; the sidebar and the command palette both read it.

## States vs flags

- **State** (lifecycle stage) is a `StatusDot`: coloured dot plus a caps mono word. `StateBadge` renders item states this way; eval outcomes and gate results too. In a feed row use `StatusLine` (dot, caps state, `·`, time).
- **Flag** (an extra fact: `git`, `endorsed`, `changed`, `excluded`) is a `.badge`. Flags are rare, so they can be loud.
- Don't show the default case when it adds nothing ("published" next to every linked item is noise).

## Interaction

- Active nav row: surface fill, hairline ring, 2px accent bar, accent icon. Selected list row: `bg-accent-soft/70` plus the accent bar. Hover: `bg-sunken`.
- `⌘K` / `Ctrl+K` opens the command palette anywhere (pages and quick actions). Every `PageHeader` shows the `⌘K` hint; the Dashboard adds the ink "New" menu (`HeaderActions`). Page-specific primary actions use `.btn-primary`, secondary ones `.btn`.
- Menus (`Dropdown`): arrow keys move, Enter selects, Esc closes and returns focus. Tooltips (`InfoTip`) open on hover and focus.
- Controls are 36px tall on phones (`h-9`), 32px from `sm`; nav rows and menu items are 40px on phones. Inputs use 14px text on phones.
- Disclosure: `Chevron`. Search: `SearchInput`. Feedback: `Feedback` (thumb icons). No emoji.
- Every number on a page comes from real data. Absent values render `–`; loading values render a skeleton; failures render `ErrorBox`.

## Components (`src/components/ui.tsx` unless noted)

| Component | For |
| --- | --- |
| `PageHeader` | page title, description, right-side actions; always renders the `⌘K` hint button before the actions |
| `SectionHeading` | icon + caps label + count + hairline rule + actions |
| `Label`, `.label-caps` | the caps mono label |
| `StatGrid`, `StatCell`, `StatGlyph`, `InfoTip` | hairline stat grids: glyph, label, (i) tooltip, value, secondary |
| `CardHeader`, `CardFooter`, `PillButton` | card lead line with actions; centred footer; outline caps pill |
| `StatusLine`, `StatusDot`, `StateBadge`, `.badge*` | states (dots) and flags (badges) |
| `Dropdown`, `MenuItem` | button + menu (the "New" menu, account menu, period selector) |
| `Kbd`, `.kbd` | key caps |
| `.btn`, `.btn-primary`, `.btn-ghost`, `.btn-danger`, `.btn-sm`, `.btn-pill`, `.input`, `.th`, `.td`, `.card` | base classes |
| `Icon`, `ICONS`, `Chevron` | 24px stroke icons at 1.7 stroke |
| `ItemKey`, `ItemLink`, `KeyLines` | item keys with dimmed folders |
| `ListDetail`, `ListRow`, `useWide`, `FilterSelect` | list + detail layout, selectable rows, breakpoint, filters |
| `TableCard`, `CardGrid`, `Crumb` | table card, auto-fill grid, breadcrumb |
| `SearchInput`, `Segmented`, `Tabs` | filtering and view switching |
| `PanelSection`, `DescList` | detail panels, key/value metadata |
| `Meter`, `Stat`, `Feedback`, `DescribedBy` | progress, standalone stat card, thumbs, doc precedence |
| `EmptyState`, `Loading`, `ErrorBox`, `Notice`, `Modal` | page states and dialogs. `EmptyState` takes the page's sidebar icon (`pageIcon("/sessions")` from `nav.ts`) and an optional `muted` second half of its heading |
| `Logo`, `SatchelMark` (`Logo.tsx`) | wordmark and mark |
| `CommandPaletteProvider`, `HeaderActions` (`CommandPalette.tsx`), `useCommandPalette` (`paletteContext.ts`) | `⌘K` palette, the ink "New" menu, and the context the header hint uses (kept in its own module to avoid an import cycle) |

## Brand marks

- **Favicon** (`public/favicon.svg`): the satchel mark in white on a cobalt (`#2F45E8`) rounded tile. The website uses the same file.
- **Charts** (`PALETTE` in `src/pages/Observability.tsx`): cobalt first, then teal, amber, violet, slate and rose at similar weight, with lighter variants for dark.
- **MCP app widgets** (`ui-widgets/src/style.css`) copy the `ink`, `muted`, `line`, `sunken`, `accent`, `good` and `bad` values from this table. Change them together.
- **Server-rendered pages** (OAuth sign-in and consent, "Satchel is running" without a UI) use `satchel/web_pages.py`: the same colours, the mark, an ink primary button and a mono eyebrow, with system fonts and inline CSS (they are served under a strict CSP and may run air-gapped). Light and dark follow the visitor's system setting.
- **The website** (getsatchel.dev) shares these tokens and adds large, light display headings and mono section eyebrows. The app keeps its own type scale: it is a tool, read in passes, not a page read once.

## Do / don't

- Do use a caps label for a heading or column header; don't use it for sentences, buttons or values.
- Do put colour in a 6–12px glyph; don't tint whole rows, cards or headers (selection and errors excepted).
- Do make the primary action ink; don't make a second primary on the same screen.
- Do say what fills an empty state and how (a CLI command or a link); don't write just "nothing here". Put the next step in the heading's muted half ("No sessions yet. *Connect an agent and ask it a data question.*") and the detail in the body.
- Don't add a font, icon set or CDN. Don't rename the product in API paths, CLI commands or `X-Satchel-*` headers from the UI.
