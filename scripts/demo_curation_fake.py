"""Run the curation loop with a *scripted* chat model (no API key needed).

    uv run --project server python scripts/demo_curation_fake.py WORKSPACE

A stand-in for ``SATCHEL_CHAT_PROVIDER``: it reads the real prompt the drafter builds and answers
with grounded JSON (a glossary term for the churn gap, an edit to the revenue guide for the
revenue gap). Use it to see the whole loop offline; with ``ANTHROPIC_API_KEY`` set, run
``SATCHEL_CHAT_PROVIDER=anthropic satchel curate run -w WORKSPACE`` instead.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import frontmatter

from satchel.curation.run import run_curation
from satchel.llm.chat import FakeChatProvider

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "examples/jaffle-shop/context/guides/revenue-reporting.md"


def respond(system: str, messages: list[Any], schema: dict[str, Any]) -> dict[str, Any]:
    if "conflict" in schema.get("properties", {}):  # hygiene contradiction check
        return {"conflict": False, "explanation": "no conflict"}
    prompt = messages[0]["content"].lower()
    if "churn" in prompt:
        return {
            "title": "Define customer churn",
            "summary": "Three users asked about churn; the context has no definition, so agents "
                       "guessed. A glossary term gives them the rule.",
            "changes": [{
                "op": "create_item", "item_type": "glossary_term",
                "key": "glossary/customer-churn.yaml",
                "proposed_body": (
                    "term: customer churn\nsynonyms: [churn, churned customer, attrition]\n"
                    "definition: >\n  A previously active customer (at least one non-returned "
                    "order) with no non-returned order in the last 90 days. Churn rate for a "
                    "period is churned customers divided by customers active at the start of "
                    "the period. Confirm the 90-day window with the finance team.\n"
                    "related_assets: [analytics.orders, analytics.customers]\n"),
                "proposed_meta": {},
                "rationale": "Users asked for churn three times and flagged the missing "
                             "definition. This reuses the existing 'active customer' window "
                             "(90 days) and flags it for confirmation.",
            }],
        }
    if "metric_divergence" in prompt or "revenue" in prompt:
        post = frontmatter.load(GUIDE)
        body = post.content.rstrip() + (
            "\n\n## Ad-hoc SQL\n\nWhen you cannot use the `sales.revenue` metric, copy its "
            "filters exactly: `status NOT IN ('returned', 'return_pending')` and "
            "`customer_id <> 0`. Revenue without them overstates the number finance reports.\n")
        return {
            "title": "Use the revenue metric's filters in ad-hoc SQL",
            "summary": "Three users computed revenue with SUM(amount) and no filters, which "
                       "includes returned and test orders.",
            "changes": [{
                "op": "update_item", "target_key": "guides/revenue-reporting.md",
                "proposed_body": body,
                "proposed_meta": {k: post.metadata[k] for k in ("name", "description", "tags")},
                "rationale": "Metric divergence signals show ad-hoc revenue SQL missing the "
                             "base model's filters.",
            }],
        }
    return {"title": "No suggestion", "summary": "Not enough to act on.", "changes": []}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    report = run_curation(sys.argv[1], chat=FakeChatProvider(respond))
    assert report is not None
    print("\n".join(report.lines()))
