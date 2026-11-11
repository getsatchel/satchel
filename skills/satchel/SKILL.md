---
name: satchel
description: Use for any data or analytics question when the Satchel MCP server is connected. Gets governed metric definitions, glossary terms and table docs before writing SQL, and reports answers, gaps and user doubt back to Satchel.
---

# Using Satchel

Satchel is a governed analytics context layer: metric definitions, glossary terms, table docs and guides for this workspace.
Workflow you MUST follow for every data question:
1. Call start_analysis with the user's question to get a session_id.
2. Call get_context_pack (or search_context) BEFORE writing any SQL, to pick up metric definitions, glossary terms and rules. Pass session_id and the user's question.
3. Use list_tables / describe_table for table details.
4. If the query tools (list_metrics, query_metrics, run_sql, get_result) are listed, answer with data: PREFER query_metrics when a governed metric exists (check list_metrics / describe_metric); only use run_sql for questions no metric covers, with a single read-only SELECT that follows the table docs (filters, units, caveats). Use get_result to page through long results. Never paste unverified numbers from memory.
5. If a term the user used has no definition in context, call flag_context_gap.
6. If the user says a number looks wrong, doubts it, corrects you or pushes back, call record_user_feedback.
7. Always finish with report_answer before replying to the user (metrics_used, tables_used, caveats).
Pass session_id and the user's original question (verbatim) on every Satchel tool call. Cite context you used by its [ctx:key@vN] tag.

## Notes
- `search_context` is for a specific term; `get_context_pack` returns everything relevant to the question in one call.
- Never invent a metric definition. If the context has none, say so, call `flag_context_gap`, and state your assumption in `report_answer.caveats`.
- If the user defines a term for you, call `propose_context_change` so a curator can review it. Proposals are never published automatically.
- `query_metrics` takes metric names from `list_metrics`. Time dimensions need a grain (`dimensions=["order_date:month"]`); filter dates with `gte`/`lt` on the time dimension (end date exclusive). `order_by` accepts `"revenue desc"`.
- `run_sql` accepts one read-only SELECT/WITH. DDL, DML and multiple statements are rejected, and results are capped (the preview shows 50 rows; page with `get_result`). Ad-hoc SQL that re-implements a defined metric without its filters is flagged to the data team, so use `query_metrics` for those.
- The query tools only appear when the workspace has a data connection.
