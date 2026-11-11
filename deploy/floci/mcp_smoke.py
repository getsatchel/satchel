"""Stdlib-only MCP smoke test over Streamable HTTP, for the floci end-to-end tests.

    python3 deploy/floci/mcp_smoke.py <base-url> <token>

initialize -> start_analysis -> run_sql (120 rows, so the result is stored) -> get_result
(page 100..109, read back from the result store). Prints the result id and exits non-zero on
any failure.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any

SQL = "select i, i * 2 as doubled from range(120) t(i) order by i"
QUESTION = "floci e2e: does a stored result read back?"


class Mcp:
    def __init__(self, base: str, token: str) -> None:
        self.url = base.rstrip("/") + "/mcp"
        self.token = token
        self.n = 0

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.n += 1
        body = {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
                "User-Agent": "floci-e2e/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode()
            ctype = resp.headers.get("Content-Type", "")
        msg = _parse(raw, ctype)
        if "error" in msg:
            raise SystemExit(f"{method}: {msg['error']}")
        return msg["result"]

    def tool(self, name: str, args: dict[str, Any]) -> Any:
        res = self.rpc("tools/call", {"name": name, "arguments": args})
        text = "".join(c.get("text", "") for c in res.get("content", []))
        if res.get("isError"):
            raise SystemExit(f"{name} failed: {text}")
        return res, text


def _parse(raw: str, ctype: str) -> dict[str, Any]:
    if "text/event-stream" not in ctype:
        return json.loads(raw)
    data = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
    for d in reversed(data):
        if d:
            return json.loads(d)
    raise SystemExit(f"no data in event stream: {raw[:200]!r}")


def main() -> None:
    base, token = sys.argv[1], sys.argv[2]
    m = Mcp(base, token)
    info = m.rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "floci-e2e", "version": "1.0"},
    })
    print(f"initialize: {info['serverInfo']['name']} {info['protocolVersion']}")
    _, text = m.tool("start_analysis", {"question": QUESTION})
    sid = json.loads(text)["session_id"]
    res, text = m.tool("run_sql", {"session_id": sid, "question": QUESTION, "sql": SQL})
    rid = (res.get("structuredContent") or {}).get("result_id")
    if not rid:
        raise SystemExit(f"run_sql returned no result_id: {text[:300]}")
    print(f"run_sql: result_id={rid}")
    res, text = m.tool("get_result", {
        "session_id": sid, "question": QUESTION, "result_id": rid, "offset": 100, "limit": 10,
    })
    sc = res.get("structuredContent") or {}
    rows = sc.get("rows") or []
    first = rows[0] if rows else None
    ok = len(rows) == 10 and first is not None and list(first.values() if isinstance(first, dict)
                                                         else first)[:2] == [100, 200]
    if not ok:
        raise SystemExit(f"get_result page is wrong: rows={rows[:2]} text={text[:300]}")
    print(f"get_result: offset 100 -> {first}")
    print(f"RESULT_ID={rid}")


if __name__ == "__main__":
    main()
