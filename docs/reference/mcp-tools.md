# MCP tools

Served at `/mcp` (streamable HTTP; PAT or OAuth) or over stdio (`satchel mcp stdio`). Tool visibility is per
principal and workspace; the data tools only appear when the workspace has a connection. Every call after
`start_analysis` carries the `session_id` it returns.

The authoritative schemas are in `server/satchel/tools/definitions.py`; clients receive them via `tools/list`.

## Session and answer

| Tool | Purpose |
|---|---|
| `start_analysis` | Begin a session for a data question. Call first, with the question verbatim. |
| `report_answer` | Required final step: record the answer, metrics and tables used, confidence, caveats. |

## Context

| Tool | Purpose |
|---|---|
| `search_context` | Search metric definitions, glossary, table docs and guides. Call before writing SQL. |
| `get_context_pack` | Packed context for a question: rules, caveats, glossary, golden queries, table docs, guides. |
| `get_item` | Read one item in full by key (from a `[ctx:key@vN]` tag). |
| `list_tables` | List documented tables. |
| `describe_table` | Columns, enums, units, grain, caveats, endorsement. |

## Governed metrics and SQL (data plane)

| Tool | Purpose |
|---|---|
| `list_metrics` | Governed metrics, measures and dimensions. |
| `describe_metric` | One metric: expression, base filters, dimensions, grains. |
| `query_metrics` | Run a governed metric query. Preferred over raw SQL. |
| `run_sql` | One read-only `SELECT`/`WITH`, parsed with sqlglot, row-capped, timed out, read-only session. |
| `get_result` | Page through a previous result by `result_id`. |

## Feedback loop

| Tool | Purpose |
|---|---|
| `flag_context_gap` | A term or metric was undefined, ambiguous or conflicting. |
| `record_user_feedback` | The user doubted, corrected or praised an answer. |
| `propose_context_change` | Propose a new or changed item. Never publishes; goes to the Inbox. |
| `rate_result` | App-only: the result widget's 👍/👎 (`source=human`). |

If the client negotiated `io.modelcontextprotocol/ui`, `run_sql`, `query_metrics` and `get_result` also return
`structuredContent` rendered by the `ui://satchel/result` widget.
