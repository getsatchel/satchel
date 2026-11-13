"""The few HTML pages the server renders itself: OAuth sign-in and consent, and "no UI built".

They share the curator UI's look (``ui/DESIGN.md``): ink on paper, one cobalt accent, light and
dark from the visitor's system setting. Everything is inline and uses system fonts: the pages are
served under ``default-src 'none'; style-src 'unsafe-inline'`` and must work air-gapped.
"""

from __future__ import annotations

import html

# Colour values copy the tokens in ui/src/index.css.
_CSS = """
:root{--bg:#f6f6f4;--surface:#fcfcfb;--sunken:#efefec;--line:#e2e2de;--ink:#0f1011;
--muted:#68696c;--accent:#2f45e8;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0f1012;--surface:#16171a;--sunken:#1d1e22;
--line:#26282d;--ink:#eeeeeb;--muted:#96989d;--accent:#8796ff;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
-webkit-font-smoothing:antialiased}
main{max-width:32rem;margin:0 auto;padding:12vh 16px 64px}
.brand{display:flex;align-items:center;gap:8px;margin-bottom:40px;color:var(--ink);
font-size:13px;font-weight:600;letter-spacing:.16em;text-transform:uppercase}
.brand svg{color:var(--accent)}
.eyebrow{display:flex;align-items:center;gap:10px;margin:0 0 14px;color:var(--muted);
font:12px/1 ui-monospace,"SF Mono",Menlo,Consolas,monospace;letter-spacing:.04em}
.eyebrow:before{content:"";width:24px;height:1px;background:currentColor;opacity:.6}
h1{margin:0 0 20px;font-size:30px;line-height:1.1;font-weight:300;letter-spacing:-.03em}
p{margin:0 0 14px}
b{font-weight:600}
a{color:var(--accent)}
code,pre{font:13px/1.5 ui-monospace,"SF Mono",Menlo,Consolas,monospace}
code{background:var(--sunken);border:1px solid var(--line);padding:1px 5px;border-radius:5px}
pre{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px;
overflow-x:auto}
ul{margin:0 0 18px;padding:0;list-style:none}
li{margin:6px 0}
.scopes li code{display:inline-block}
.note{color:var(--muted);font-size:13px;border-top:1px solid var(--line);padding-top:14px;
margin-top:22px}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:22px}
button{font:inherit;font-size:14px;font-weight:500;min-height:40px;padding:0 18px;
border-radius:8px;border:1px solid var(--line);background:var(--surface);color:var(--ink);
cursor:pointer}
button:hover{background:var(--sunken)}
button.primary{background:var(--ink);border-color:var(--ink);color:var(--bg)}
button.primary:hover{opacity:.88}
button:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.choices button{width:100%;text-align:left;min-height:48px}
"""

# The satchel mark (ui/src/components/Logo.tsx), drawn in the accent.
_MARK = (
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M8.5 7V5.6A1.6 1.6 0 0 1 10.1 4h3.8a1.6 1.6 0 0 1 1.6 1.6V7"/>'
    '<path d="M3.5 9.2A2.2 2.2 0 0 1 5.7 7h12.6a2.2 2.2 0 0 1 2.2 2.2v8.6a2.2 2.2 0 0 1-2.2 '
    '2.2H5.7a2.2 2.2 0 0 1-2.2-2.2z"/>'
    '<path d="M3.5 9.5c0 2.2 1.6 3.7 3.8 3.7h9.4c2.2 0 3.8-1.5 3.8-3.7"/>'
    '<path d="M10.6 12.2h2.8v3.2h-2.8z"/></svg>'
)


def shell(title: str, body: str, *, eyebrow: str | None = None) -> str:
    """A whole page: the wordmark, an optional eyebrow, the title as the heading, then ``body``
    (already escaped HTML)."""
    t = html.escape(title)
    lead = f"<p class=eyebrow>{html.escape(eyebrow)}</p>" if eyebrow else ""
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{t} · Satchel</title><style>{_CSS}</style>"
        f"<main><div class=brand>{_MARK}Satchel</div>{lead}<h1>{t}</h1>{body}</main></html>"
    )
