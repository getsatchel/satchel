"""Generate a little agent activity against a running demo, so the UI has data to show.

    SATCHEL_TOKEN=satchel_pat_... uv run --project server python scripts/demo_activity.py [URL]

Plays a few scripted analyses over MCP (Streamable HTTP) as different clients: context packs,
SQL (one deliberately wrong), answers, feedback, a flagged gap and a proposed change.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Implementation

URL = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080") + "/mcp"
TOKEN = os.environ["SATCHEL_TOKEN"]

Call = tuple[str, dict[str, Any]]


async def play(client: str, question: str, steps: list[Call]) -> None:
    # satchel names the client from its User-Agent (stateless HTTP), like claude-code/2.1 does.
    headers = {"Authorization": f"Bearer {TOKEN}", "User-Agent": f"{client}/1.0"}
    async with streamablehttp_client(URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w, client_info=Implementation(name=client, version="1.0")) as s:
            await s.initialize()
            res = await s.call_tool("start_analysis", {"question": question})
            sid = json.loads(res.content[0].text)["session_id"]  # type: ignore[union-attr]
            for tool, args in steps:
                out = await s.call_tool(tool, {"session_id": sid, "question": question, **args})
                flag = " (error)" if out.isError else ""
                print(f"  {client:<15} {tool}{flag}")


GOOD_SQL = (
    "select date_trunc('month', order_date) as month, sum(amount) / 100.0 as revenue "
    "from analytics.orders where status not in ('returned', 'return_pending') "
    "group by 1 order by 1 desc limit 6"
)
BAD_SQL = "select sum(total_cents) from analytics.orders"  # no such column: logs a sql_error


async def main() -> None:
    plays: list[tuple[str, str, list[Call]]] = [
        (
            "claude-desktop",
            "What was our revenue by month, excluding returns?",
            [
                ("get_context_pack", {}),
                ("run_sql", {"sql": BAD_SQL}),
                ("run_sql", {"sql": GOOD_SQL}),
                ("report_answer", {
                    "answer_summary": "Monthly revenue excluding returned orders.",
                    "tables_used": ["analytics.orders"], "confidence": "high",
                }),
                ("record_user_feedback", {"sentiment": "positive", "comment": "Exactly what I needed."}),
            ],
        ),
        (
            "claude-code",
            "How many active customers do we have?",
            [
                ("search_context", {"query": "active customer"}),
                ("get_context_pack", {}),
                ("flag_context_gap", {
                    "what_was_missing": "Which date column defines 'active' for guest checkouts",
                    "why": "Guest orders have no customer id, so they cannot be counted",
                    "kind": "ambiguous_term",
                }),
                ("report_answer", {
                    "answer_summary": "Customers with an order in the last 90 days.",
                    "tables_used": ["analytics.customers"], "confidence": "medium",
                    "caveats": ["Guest checkouts are excluded"],
                }),
            ],
        ),
        (
            "cursor",
            "What is our refund rate and does it include pending returns?",
            [
                ("search_context", {"query": "refund rate"}),
                ("get_context_pack", {}),
                ("record_user_feedback", {
                    "sentiment": "correction",
                    "comment": "Refund rate must exclude return_pending orders.",
                }),
                ("propose_context_change", {
                    "op": "create_item", "type": "caveat",
                    "body": "The refund rate excludes orders in return_pending: only completed "
                            "refunds count.",
                    "meta": {"applies_to": ["analytics.orders"]},
                    "rationale": "A user corrected the answer in this session.",
                }),
            ],
        ),
        (
            "claude-desktop",
            "Who are our top 5 customers by lifetime spend?",
            [
                ("get_context_pack", {}),
                ("report_answer", {
                    "answer_summary": "Top customers by lifetime spend.",
                    "tables_used": ["analytics.orders", "analytics.customers"],
                    "confidence": "high",
                }),
                ("record_user_feedback", {"sentiment": "positive", "comment": "Great."}),
            ],
        ),
    ]
    for client, question, steps in plays:
        print(f"{client}: {question}")
        await play(client, question, steps)


asyncio.run(main())
