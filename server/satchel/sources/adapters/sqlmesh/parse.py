"""A static reader for SQLMesh ``MODEL (...)`` blocks and ``@model(...)`` decorators.

SQLMesh extends the SQL grammar with a ``MODEL`` statement; plain sqlglot cannot parse it, and
loading a SQLMesh ``Context`` would execute the project's Python (``config.py``, macros, Python
models) inside the satchel server and pin ``sqlglot~=30.8`` (satchel needs >= 30.25). So this reader
extracts the block with a small bracket-aware scanner and reads its properties
(``name``, ``kind``, ``grain``, ``audits (not_null(columns := (a)))``, ``column_descriptions (a =
'...')``, ...) into plain Python values. Python models are read with :mod:`ast` (never executed).
Nothing here raises on odd input: a block that cannot be read is reported by the caller.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<str>'(?:[^']|'')*')
      | (?P<qid>"(?:[^"]|"")*"|`[^`]*`)
      | (?P<num>-?\d+(?:\.\d+)?)
      | (?P<assign>:=|=)
      | (?P<punct>[()\[\],])
      | (?P<ident>@?\{?[A-Za-z_][\w$]*\}?(?:\s*\.\s*(?:[A-Za-z_][\w$]*|"(?:[^"]|"")*"|`[^`]*`))*)
      | (?P<other>.)
    )""",
    re.VERBOSE | re.DOTALL,
)
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_MODEL_START = re.compile(r"(?im)^\s*MODEL\s*\(")


class ParseError(ValueError):
    pass


@dataclass
class Call:
    """``not_null(columns := (a))`` -> name ``not_null``, kwargs ``{"columns": ["a"]}``."""

    name: str
    args: list[Any] = field(default_factory=list)
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Block:
    props: dict[str, Any]
    raw: dict[str, str]  # property -> raw value text
    query: str  # the SQL after the MODEL block


def strip_comments(sql: str) -> str:
    """Remove ``--`` and ``/* */`` comments (outside of string literals)."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"`":
            j = _skip_quoted(sql, i)
            out.append(sql[i:j])
            i = j
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _skip_quoted(s: str, i: int) -> int:
    q = s[i]
    j = i + 1
    while j < len(s):
        if s[j] == q:
            if q != "`" and s[j + 1:j + 2] == q:  # doubled quote escapes itself
                j += 2
                continue
            return j + 1
        j += 1
    return len(s)


def _matching(s: str, start: int) -> int:
    """Index of the ``)`` that closes the ``(`` at ``start``."""
    depth = 0
    i = start
    while i < len(s):
        ch = s[i]
        if ch in "'\"`":
            i = _skip_quoted(s, i)
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ParseError("unbalanced parentheses in MODEL block")


def split_top(s: str) -> list[str]:
    """Split on commas that are not inside brackets or quotes."""
    parts: list[str] = []
    depth, cur, i = 0, 0, 0
    while i < len(s):
        ch = s[i]
        if ch in "'\"`":
            i = _skip_quoted(s, i)
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(s[cur:i])
            cur = i + 1
        i += 1
    parts.append(s[cur:])
    return [p.strip() for p in parts if p.strip()]


def has_model_block(sql: str) -> bool:
    return bool(_MODEL_START.search(strip_comments(sql)))


def parse_model(sql: str) -> Block:
    """The MODEL block of a ``.sql`` file and the query that follows it."""
    text = strip_comments(sql)
    m = _MODEL_START.search(text)
    if m is None:
        raise ParseError("no MODEL block")
    start = m.end() - 1
    end = _matching(text, start)
    inner = text[start + 1:end]
    props: dict[str, Any] = {}
    raw: dict[str, str] = {}
    for item in split_top(inner):
        km = re.match(r"([A-Za-z_]\w*)\s*(.*)", item, re.DOTALL)
        if km is None:
            continue
        key, rest = km.group(1).lower(), km.group(2).strip()
        raw[key] = rest
        try:
            props[key] = parse_value(rest, props_inside=key == "kind") if rest else True
        except ParseError:
            props[key] = rest
    query = text[end + 1:].lstrip().lstrip(";").strip()
    return Block(props, raw, query)


# ---- values ---------------------------------------------------------------------------------


def _tokens(s: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(s):
        m = _TOKEN.match(s, pos)
        if m is None or m.end() == pos:
            break
        pos = m.end()
        kind = m.lastgroup or "other"
        if kind == "other" and not m.group("other").strip():
            continue
        out.append((kind, m.group(kind)))
    return out


class _Parser:
    def __init__(self, toks: list[tuple[str, str]], props: bool = False) -> None:
        self.t, self.i = toks, 0
        self.props = props  # inside ``KIND (key (a, b), key2 value)``: ``key (..)`` is a pair

    def peek(self) -> tuple[str, str] | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise ParseError("unexpected end of value")
        self.i += 1
        return tok

    def value(self) -> Any:
        kind, text = self.take()
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "qid":
            return text[1:-1]
        if kind == "num":
            return float(text) if "." in text else int(text)
        if kind == "ident":
            nxt = self.peek()
            if nxt == ("punct", "("):
                self.take()
                args, kwargs = self.args(")", props=False)
                return Call(text, args, kwargs)
            return {"true": True, "false": False}.get(text.lower(), text)
        if kind == "punct" and text in "([":
            args, kwargs = self.args(")" if text == "(" else "]")
            return kwargs if kwargs else args
        raise ParseError(f"unexpected {text!r}")

    def args(self, close: str, props: bool | None = None) -> tuple[list[Any], dict[str, Any]]:
        props = self.props if props is None else props
        args: list[Any] = []
        kwargs: dict[str, Any] = {}
        while True:
            tok = self.peek()
            if tok is None:
                raise ParseError("missing closing bracket")
            if tok == ("punct", close):
                self.take()
                return args, kwargs
            if tok == ("punct", ","):
                self.take()
                continue
            nxt = self.t[self.i + 1] if self.i + 1 < len(self.t) else None
            if props and tok[0] == "ident" and nxt == ("punct", "("):
                self.take()
                self.take()
                a, k = self.args(")", props=False)
                kwargs[tok[1].lower()] = k or a
            elif tok[0] in ("ident", "str", "qid") and nxt is not None and nxt[0] == "assign":
                self.take()
                self.take()
                kwargs[tok[1].strip("'\"`").lower() if tok[0] != "str" else tok[1][1:-1]] = \
                    self.value()
            elif tok[0] == "ident" and nxt is not None and (
                nxt[0] in ("ident", "str", "num") or nxt == ("punct", "(")
            ) and not _is_call(self.t, self.i):
                # ``time_column ds`` inside a kind: a key and a value without ``=``
                self.take()
                kwargs[tok[1].lower()] = self.value()
            else:
                args.append(self.value())


def _is_call(toks: list[tuple[str, str]], i: int) -> bool:
    return i + 1 < len(toks) and toks[i + 1] == ("punct", "(")


def parse_value(text: str, props_inside: bool = False) -> Any:
    """``'x'``, ``a.b``, ``[..]``, ``(..)``, ``name(args)`` to Python. With ``props_inside`` the
    value is ``KIND (key value, key (a, b))`` as in a model ``kind``."""
    toks = _tokens(text)
    if not toks:
        return None
    p = _Parser(toks, props_inside)
    if props_inside and toks[0][0] == "ident" and len(toks) > 1 and toks[1] == ("punct", "("):
        head = p.take()[1]
        p.take()
        args, kwargs = p.args(")", props=True)
        return Call(head, args, kwargs)
    return p.value()


def parse_columns(raw: str) -> list[tuple[str, str, bool]]:
    """``(a INT, b TEXT NOT NULL)`` -> ``[("a", "INT", False), ("b", "TEXT", True)]``."""
    raw = raw.strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    out: list[tuple[str, str, bool]] = []
    for item in split_top(raw):
        m = re.match(r'("(?:[^"]|"")*"|`[^`]*`|[A-Za-z_]\w*)\s+(.+)', item, re.DOTALL)
        if m:
            kind = " ".join(m.group(2).split())
            required = bool(re.search(r"(?i)\bnot\s+null$", kind))
            kind = re.sub(r"(?i)\s*\bnot\s+null$", "", kind) if required else kind
            out.append((m.group(1).strip('"`'), kind, required))
    return out


# ---- python models --------------------------------------------------------------------------


def python_models(source: str) -> list[dict[str, Any]]:
    """Properties of the ``@model("name", columns={...}, ...)`` functions of a Python file.

    Only literal arguments are read (``ast.literal_eval``); ``kind`` may be written as
    ``dict(name=ModelKindName.FULL)``. The file is never imported or executed."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise ParseError(f"not valid Python: {e.msg}") from None
    out: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and _name(dec.func) in ("model", "sqlmesh.model")):
                continue
            props: dict[str, Any] = {}
            if dec.args:
                props["name"] = _literal(dec.args[0])
            for kw in dec.keywords:
                if kw.arg:
                    props[kw.arg.lower()] = _literal(kw.value)
            out.append(props)
    return out


def _name(n: ast.expr) -> str:
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute):
        return f"{_name(n.value)}.{n.attr}"
    return ""


def _literal(n: ast.expr) -> Any:
    try:
        return ast.literal_eval(n)
    except (ValueError, SyntaxError):
        pass
    if isinstance(n, ast.Call) and _name(n.func) == "dict":
        return {k.arg: _literal(k.value) for k in n.keywords if k.arg}
    if isinstance(n, ast.Attribute):
        return n.attr  # ModelKindName.FULL -> "FULL"
    if isinstance(n, ast.Name):
        return n.id
    return None
