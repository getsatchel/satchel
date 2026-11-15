# Satchel UI design standard

How the curator UI looks, behaves and speaks. Read [`PRODUCT.md`](PRODUCT.md) first: it says who the UI is for and what earns space on a page. This file says how to build it.

The code is the source of truth: tokens in `src/index.css` and `tailwind.config.ts`, components in `src/components/ui.tsx`. If this file and the code disagree, fix whichever is wrong in the same change. Use an existing component or class before writing new markup; if nothing fits, add the part to `ui.tsx` and to the [component index](#component-index).

## Principles

The look is **ink on paper**: near-black text on off-white, neutral greys, one cobalt accent. People scan lists, tables and diffs here to decide what to change, so:

1. **The thing to act on is the first thing read.** One primary action per screen, in ink. Everything else is quieter.
2. **Colour means something.** It appears in small glyphs, dots and badges, and only when a thing differs from normal or needs a person.
3. **Dense, not cramped.** 13px body text and tight rows, with space between sections, not inside them.
4. **One way to do each thing.** A choice, a status, a dialog and an empty page each have one pattern. A second way needs a reason written here.
5. **Plain words.** A label names the thing. Help states a constraint. Nothing on screen sells or reassures.

## Foundations

### Colour

Tokens are RGB triplets (so Tailwind can apply alpha, `bg-accent/40`), switched by `data-theme` on `<html>` through `src/lib/theme.ts`.

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `backdrop` | `#EAEAE7` | `#08090A` | the ground behind the app panel, from `md` |
| `bg` | `#F6F6F4` | `#0F1012` | panel, sidebar, page |
| `surface` | `#FCFCFB` | `#16171A` | cards, inputs, menus, dialogs |
| `sunken` | `#EFEFEC` | `#1D1E22` | hover, table header tint, skeletons, code |
| `line` | `#E2E2DE` | `#26282D` | every border and hairline |
| `ink` | `#0F1011` | `#EEEEEB` | body text |
| `muted` | `#68696C` | `#96989D` | secondary text, labels |
| `primary` / `primary-ink` | ink / bg | near-white / dark | the primary button, the selected segment |
| `accent` / `accent-soft` | `#2F45E8` / `#E9ECFD` | `#8796FF` / `#1F2344` | links, focus ring, selection, the active nav row, endorsed |
| `good` | `#1F7D4C` | `#6AC48A` | healthy, live, passed |
| `warn` | `#AB680B` | `#E4B054` | needs a person: drafts, unpublished, pending |
| `bad` | `#BE3226` | `#F08070` | broken, failed, excluded |
| `info` | `#4F5BD5` | `#93A0F5` | agent-originated, neutral activity |

Each tone has a `-soft` background for badges and error boxes.

- **Ink is what to do; accent is where you are.** Primary buttons are ink. Accent marks links, focus, the selected row and the active page.
- **Tones are never decoration.** "Needs attention" is always `warn`. A finished step, a saved form and a default value get no colour.
- **Never fill a card, header or row with a tone.** The exceptions are the selected list row (`bg-accent-soft/70`) and error boxes.
- **Neutrals stay neutral.** The greys sit a hair warm of pure grey so they never read cold; no cream, sand or brown tint.
- Text contrast on `bg` and `surface` is at least 4.5:1 for `ink`, `muted`, `accent`, `good`, `bad` and `info` in both themes. Light `warn` is the exception (see [Known gaps](#known-gaps)).
- Charts use `PALETTE` in `src/pages/Usage.tsx`: cobalt first, then teal, amber, violet, slate and rose, with lighter variants for dark.

### Type

**Geist** for text, **Geist Mono** for identifiers and caps labels. Both are bundled (`@fontsource-variable/*`); never load a font from a CDN, because the UI ships in the wheel and may run air-gapped.

| Class | Size | Use |
| --- | --- | --- |
| `text-2xs` | 11px | caps labels only |
| `text-xs` | 12px | metadata, help text, badges |
| `text-sm` | 13px | body, tables, buttons |
| `text-base` | 14px | prose, inputs on phones |
| `text-lg` | 18px | stat values, empty-state headings |
| `text-xl` | 20px | the value in a standalone `Stat` |
| page title | 22px, 26px from `sm` | `PageHeader` only: `font-light`, `tracking-[-0.03em]` |

- **Caps labels** (`.label-caps`, `Label`: mono, 11px, uppercase, tracked, muted) are for section headings, sidebar group labels, stat labels, table column headers, panel sub-headings, status words and pill buttons. Nothing else is uppercase.
- Identifiers (item keys, table and column names, SQL types, versions, tool names) are `font-mono`. Numbers that line up get `tabular-nums`.
- One large line per page: the `PageHeader` title. Do not add a second display size.

### Space, shape and depth

- **Spacing.** Sections are `space-y-9` apart. Cards pad `px-4 py-3` to `p-4`; table cells are `.td` (`px-3 py-2.5`). Form fields are `space-y-4`. Use the Tailwind scale, not arbitrary pixel values.
- **Radius.** `rounded` 6px for badges and small buttons, `rounded-md` 8px for buttons and inputs, `rounded-lg` 10px for menus, `rounded-xl` 12px for cards and dialogs, `rounded-2xl` 16px for the app panel, `rounded-full` for pills and dots.
- **Depth.** Borders do the work; shadows are for things that float. `shadow-panel` on the app panel, `shadow-pop` on dialogs, menus and tooltips, and a shadow on the phone drawer and on toasts. Cards have no shadow. No gradients or glow; the only blur is the faint one behind the command palette.
- **Motion.** Colour transitions on hover and focus, the drawer slide (200ms) and the skeleton pulse. Nothing else moves, and all of it stops under `prefers-reduced-motion`.
- **Icons.** `Icon` with a path from `ICONS`: 24px grid, 1.7 stroke, no fills. No emoji, no second icon set. An icon-only button has an `aria-label`.

## Frame and navigation

- **Frame.** From `md` the app is one rounded panel on the flat `backdrop`, with a 12 to 16px margin. Below `md` it is full-bleed with a 48px top bar and a slide-in drawer.
- **Sidebar.** Wordmark, workspace row, Search (`⌘K`), Dashboard, then the groups. Each group has a short coloured dash and a caps label. Bottom: Docs ↗ (`DOCS_URL` in `src/components/nav.ts`) and the account row (menu: theme, sign out).
- **Active nav row:** surface fill, hairline ring, 2px accent bar, accent icon.

| Group | Pages |
| --- | --- |
| (top) | Dashboard (`/dashboard`, the default route) |
| CURATE | Inbox, Library, Releases (Changes, History) |
| OBSERVE | Sessions (Sessions, Usage), Evals |
| DATA | Catalog, Sources |
| ADMIN | Settings (Models, Connections, Members, Jobs, Limits, Deployment) |

Keep the sidebar short: a new view of something an existing page covers is a tab on that page (`Tabs`, `?tab=` in the URL), not a new entry. Add a page by adding it to `NAV_GROUPS` in `src/components/nav.ts`; the sidebar and the command palette both read it. A tab people look for by name goes in `SUB_PAGES` so the palette finds it, and a page that becomes a tab keeps its old URL as a redirect (`Moved` in `src/App.tsx`: `/changes`, `/observability`, `/jobs`). `⌘K` / `Ctrl+K` opens the palette anywhere, and every `PageHeader` shows the hint. The Dashboard adds the ink "New" menu (`HeaderActions`).

## Pages

Every page is a `PageHeader` (title, one-line description, actions on the right), then sections, each opened by a `SectionHeading` (icon, caps label, optional count, hairline rule, optional actions).

| Kind | Built from | Examples |
| --- | --- | --- |
| Overview | `StatGrid` in a card, cards in a `CardGrid`, a `PillButton` in a `CardFooter` to go deeper | Dashboard, Sessions → Usage |
| List | filters, `TableCard`, paging in its footer | Sessions, Releases → History, Settings → Jobs |
| List and detail | `ListDetail`: side by side from `lg` or `xl`, one pane with Back below | Inbox, Catalog |
| Detail | `Crumb`, `PageHeader`, sections; a narrow sticky column on the right for secondary lists | a session, a source, an item |
| Tabbed | `Tabs` under the `PageHeader`, one view per tab, the default tab first with no `?tab=` | Settings, Releases, Sessions |

Put the layers from PRODUCT.md where they belong: state and the next action in the default view, setup and history in an expanded row or panel, raw payloads and IDs behind a `<details>` or an Expand control.

## Patterns

### Tables and lists

- The primary column is `w-full`; the others are `w-px whitespace-nowrap`. Hide less important columns at smaller breakpoints and fold their values under the primary cell on phones.
- A row that opens something has a real link in its primary cell; the row click is a convenience on top.
- Filters sit above the table: a `Segmented` to switch what the rows are, `FilterSelect` to narrow them. Filter state lives in the URL.
- A selected row in a list beside a detail is `ListRow`: `bg-accent-soft/70` and a 2px accent bar. Hover is `bg-sunken`.

### States and flags

- **State** (a lifecycle stage or an outcome) is a `StatusDot`: a coloured dot and a caps mono word. `StateBadge` renders item states this way. In a feed row use `StatusLine` (dot, state, `·`, time).
- **Flag** (an extra fact: `git`, `endorsed`, `sensitive`, `error`) is a `.badge`. Flags are rare, so they can be loud.
- Do not show the default. "Published" beside every item is noise; a value that is the same on every row belongs in the detail.

### Empty, loading and failed

- **Empty:** `EmptyState` with the page's sidebar icon (`pageIcon("/sessions")`). The heading says what is missing, its muted half says what to do ("No sources yet. *Add the dbt, Cube or SQLMesh project that describes your warehouse.*"), and the body gives the detail or the CLI command.
- **Loading:** `Loading` for a page, a skeleton for a single value. Never show a zero or an undone state while the answer is still loading.
- **Failed:** `ErrorBox` with the server's message, where the content would have been. Absent values render `–`.
- Every number on a page comes from real data.

### Forms

- **Field anatomy.** `.label` above, the control (`.input`, `Segmented`, a checkbox), then at most one line of `text-xs text-muted` help. Help states a constraint or a consequence the label cannot ("Cannot be changed later"). It never restates the label or explains the product; longer explanation is an `InfoTip` or a "Setup guide" link to the docs.
- **Choosing one of a few** is a `Segmented` for up to five short options, else a `<select>`. An option that cannot be picked is disabled, with the reason as its tooltip. Do not draw choices as cards.
- **Secrets** are password inputs (a masked textarea for key files), say "Encrypted at rest and never shown again", and show "Stored. Leave empty to keep it." when one exists.
- **Errors** appear under the form in an `ErrorBox`; a check (Test) shows its result beside the button that ran it.
- Group a long form with caps-label fieldsets separated by a hairline.

### Dialogs

- `Modal`: title, body, footer. `max-w-md` for a short form, `wide` for a two-column one. Every dialog has the Close button in its header; on phones it is a bottom sheet.
- **Footer order**, left to right: a secondary check (Test) on the far left, then Cancel, then the one ink button. Button words are the verb of the result: Save, Remove, Publish, Crawl. A destructive confirm uses `.btn-danger` and names the thing in its title ("Remove warehouse?").
- **Steps are for length, not ceremony.** Split a dialog into steps only when one form would scroll for several screens (adding a source). Show "Step 2 of 3" under the title and nothing else: no step names, ticks or progress bar. Back keeps what was entered, Next validates the step, a stepped dialog does not close on an outside click, and focus moves to the first field of each step. Editing the same thing shows the whole form.
- **After a create**, say what to do next once: a second state of the same dialog with one action (a new connection offers Crawl), or the new thing's own page (a new source). Not both, and no "success" step.
- A flow that needs more than three steps, or that people come back to, is a page, not a dialog.

### Feedback

- `useToast()` for the result of an action: `ok` for done, `err` for failed (it stays longer). A toast states what happened in one sentence and never holds the only copy of something a person needs later.
- A result that belongs to a place on the page (a test, a sync) is shown there, not only in a toast.

## Voice

- Labels are nouns ("Kind", "Name", "Warehouse connection"), not questions. Headings and buttons are sentence case.
- One plain sentence says what a thing is or does. No reassurance ("ready to", "straight away"), no narration of what the user just did ("pg is connected"), no selling.
- Use the product's words (PRODUCT.md, Terminology) and the name of the action ("Crawl", "Sync", "Publish"), never a new synonym.
- A sentence that is true of every row, field or step belongs in the docs, not on the screen.
- When the fix is outside the UI, give the command (`satchel source add …`) instead of a control that fails.
- Never rename the product in API paths, CLI commands or `X-Satchel-*` headers.

## Responsive

- No horizontal page scroll at 390px. `TableCard` scrolls inside itself instead of clipping.
- No fixed column grid without a breakpoint. `CardGrid` for cards; `StatGrid` is two columns on phones.
- Breakpoints are Tailwind's (`sm` 640, `md` 768, `lg` 1024, `xl` 1280). The drawer replaces the sidebar below `md`; list and detail split from `lg` or `xl`.
- Controls are 36px tall on phones (`h-9`) and 32px from `sm`; nav rows and menu items are 40px on phones. Inputs use 14px text on phones.
- Do not auto-select a list row on a narrow screen: it would hide the list.

## Accessibility

- Everything works from the keyboard. The focus ring is the 2px accent outline from `index.css`; never remove it.
- Menus (`Dropdown`): arrow keys move, Enter selects, Esc closes and returns focus. Tooltips (`InfoTip`) open on hover and on focus.
- Dialogs are native `<dialog>` elements labelled by their title; focus goes to the first field.
- Colour is never the only signal: a state is a dot and a word, an error is a badge and a message.
- Results that appear without a page change use `role="status"`; failures use `role="alert"`.
- The shell has a "Skip to content" link; a page has one `h1` (the `PageHeader`) and `h2` section headings.

## Component index

All in `src/components/ui.tsx` unless noted.

| Component | For |
| --- | --- |
| `PageHeader`, `Crumb` | page title, description and actions; the breadcrumb above a detail page |
| `SectionHeading`, `Label`, `.label-caps` | section opener; the caps mono label |
| `.card`, `CardHeader`, `CardFooter`, `CardGrid`, `PillButton` | cards, their lead line, the centred footer and its outline caps pill |
| `StatGrid`, `StatCell`, `StatGlyph`, `Stat`, `Meter` | stat grids, a standalone stat, progress |
| `TableCard`, `.th`, `.td` | tables |
| `ListDetail`, `ListRow`, `useWide` | list beside detail, selectable rows, the breakpoint hook |
| `FilterSelect`, `SearchInput`, `Segmented`, `Tabs` | filtering, switching views, choosing one of a few |
| `StatusDot`, `StatusLine`, `StateBadge`, `.badge*`, `TypeBadge`, `GitBadge`, `SourceBadge` | states and flags |
| `.btn`, `.btn-primary`, `.btn-ghost`, `.btn-danger`, `.btn-sm`, `.btn-pill`, `.input`, `.label` | buttons and fields |
| `Modal` | dialogs, optionally stepped |
| `Dropdown`, `MenuItem`, `InfoTip`, `Kbd` | menus, tooltips, key caps |
| `EmptyState`, `Loading`, `ErrorBox`, `Notice` | page states and inline notices |
| `PanelSection`, `DescList` | detail panels, key and value metadata |
| `ItemKey`, `ItemLink`, `KeyLines`, `DescribedBy` | item keys and links |
| `Feedback` | thumbs counts |
| `Icon`, `ICONS`, `Chevron` | icons and disclosure |
| `useToast` (`Toast.tsx`) | action results |
| `CodeEditor`, `Pre` (`Code.tsx`), `UnifiedDiff`, `Markdown` | code, diffs, rendered markdown |
| `CommandPaletteProvider`, `HeaderActions` (`CommandPalette.tsx`), `useCommandPalette` (`paletteContext.ts`) | the `⌘K` palette and the Dashboard's "New" menu |
| `Logo`, `SatchelMark` (`Logo.tsx`) | wordmark and mark |

## Outside the app

These share the tokens above. Change them together.

- **Favicon** (`src/assets/favicon.svg`): the mark in white on a cobalt rounded tile. The website uses the same file.
- **MCP app widgets** (`ui-widgets/src/style.css`) copy `ink`, `muted`, `line`, `sunken`, `accent`, `good` and `bad`.
- **Server-rendered pages** (`satchel/web_pages.py`: OAuth sign-in and consent, "Satchel is running"): the same colours and mark, with system fonts and inline CSS because they are served under a strict CSP. They follow the visitor's system theme.
- **The website** (getsatchel.dev) adds large light display headings and mono eyebrows. The app keeps its own scale: it is a tool, read in passes.

## Before you open a pull request

- The page works at 390px and at 1280px, in light and dark.
- There is one ink button on the screen, and no new colour, radius, shadow, font or icon set.
- Every state is handled: loading, empty, failed, and what a lower role sees.
- New text follows [Voice](#voice): labels are nouns and help is one line.
- It can be used from the keyboard, and focus is visible throughout.
- `npm run typecheck && npm run lint && npm run build` pass.

## Known gaps

- **Touch targets** are 36px on phones; the common guideline is 44px.
- **Light `warn`** text is about 4.2:1 on `bg` and `surface`, under the 4.5:1 target for small text. Darkening it changes a token the website shares.
- **Placeholder text** (`muted` at 70%) is below 4.5:1.
- **Dialogs on phones** (the bottom sheet) have not been checked on a narrow screen.
- **No automated UI tests:** only review checks these rules.
