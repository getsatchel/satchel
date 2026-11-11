"""Generate realistic *gap* activity for the curation loop (M4) against a running satchel.

    SATCHEL_TOKENS=tokA,tokB,tokC uv run --project server python scripts/demo_curation_activity.py [URL]

Plays scripted analyses over MCP as three different users (one token each):
- all three ask about customer churn, which the Jaffle Shop context does not define, and flag
  the gap in their own words;
- all three compute revenue ad hoc without the metric's filters (metric divergence), and one
  gets corrected by the user;
- the first user also has a clean, positively rated analysis (a golden-query candidate).
Then run ``satchel curate run`` (or POST /v1/curate/run) to turn this into suggestions.
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
TOKENS = [t for t in os.environ["SATCHEL_TOKENS"].split(",") if t]
Call = tuple[str, dict[str, Any]]

ADHOC_REVENUE = "select sum(amount) / 100.0 as revenue from analytics.orders"
GOOD_SQL = (
    "select date_trunc('month', order_date) as month, sum(amount) / 100.0 as revenue "
    "from analytics.orders where status not in ('returned', 'return_pending') "
    "and customer_id <> 0 group by 1 order by 1 desc limit 6"
)

CHURN = [
    ("What is our monthly customer churn rate?",
     "No definition of churn in the context: I do not know what counts as a churned customer",
     "Churn rate could not be computed without an agreed definition"),
    ("How many customers did we lose last quarter?",
     "Churn is not defined anywhere. Is it no order in 90 days, or an explicit cancellation?",
     "Customer loss count depends on how churn is defined"),
    ("Which customers have churned recently?",
     "Missing definition of a churned customer and of the churn window",
     "List of churned customers is a guess without a definition"),
]


async def play(token: str, question: str, steps: list[Call]) -> None:
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "claude-desktop/1.0"}
    async with streamablehttp_client(URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w, client_info=Implementation(name="claude-desktop",
                                                                  version="1.0")) as s:
            await s.initialize()
            res = await s.call_tool("start_analysis", {"question": question})
            sid = json.loads(res.content[0].text)["session_id"]  # type: ignore[union-attr]
            for tool, args in steps:
                out = await s.call_tool(tool, {"session_id": sid, "question": question, **args})
                print(f"  {tool}{' (error)' if out.isError else ''}")


async def main() -> None:
    assert len(TOKENS) >= 3, "SATCHEL_TOKENS needs three comma-separated PATs"
    for i, (token, (q, missing, caveat)) in enumerate(zip(TOKENS, CHURN, strict=False)):
        print(f"user {i + 1}: {q}")
        await play(token, q, [
            ("get_context_pack", {}),
            ("flag_context_gap", {"what_was_missing": missing, "kind": "missing_context",
                                  "why": "The agent had to guess the business rule"}),
            ("report_answer", {"answer_summary": "Provisional answer", "confidence": "low",
                               "caveats": [caveat]}),
        ])
    for i, token in enumerate(TOKENS[:3]):
        q = ["What was total revenue?", "How much revenue have we made?",
             "Give me the revenue figure for the board pack"][i]
        print(f"user {i + 1}: {q}")
        steps: list[Call] = [("get_context_pack", {}), ("run_sql", {"sql": ADHOC_REVENUE})]
        if i == 0:
            steps.append(("record_user_feedback", {
                "sentiment": "correction",
                "comment": "This is higher than finance's number: returned orders and the "
                           "test customer must be excluded"}))
        await play(token, q, steps)
    q = "What was revenue by month, excluding returns and test orders?"
    print(f"user 1: {q}")
    await play(TOKENS[0], q, [
        ("get_context_pack", {}), ("run_sql", {"sql": GOOD_SQL}),
        ("report_answer", {"answer_summary": "Monthly revenue, valid orders only",
                           "tables_used": ["analytics.orders"], "confidence": "high"}),
        ("record_user_feedback", {"sentiment": "positive", "comment": "Exactly right"}),
    ])


asyncio.run(main())
