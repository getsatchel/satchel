# Observability

**Observability** (under Observe, for curators and admins) shows how agents use Satchel: who
asks, how good and how grounded the answers are, how fast the tools are, and whether agents follow
Satchel's workflow. Every number comes from what Satchel already records: sessions and their
questions, tool calls (with latency and errors), the arguments of `report_answer` and
`query_metrics`, feedback and signals. Eval sessions are left out.

Pick a period of 7, 30 or 90 days. Headline numbers show their change from the period before it:
a count as a percentage, a rate in points. The Dashboard's **Agent activity** card shows four of
them, each linking to its tab.

A **question** is one turn of a session: agents often keep a session for follow-ups, and Satchel
starts a new turn when a call carries a different question.

## Tabs

| Tab | What it shows |
|---|---|
| **Overview** | Questions, people, thumbs-up rate, answered rate, governed rate, adherence, p95 latency and gaps flagged; questions per day by client; signals by kind; the most-served context items. |
| **Quality** | Thumbs up and down per week and by client; the share of answered questions a person doubted or corrected (a `user_doubt` signal); answers reported with low confidence or with caveats; the latest thumbs down with their comments. |
| **Adoption** | Questions per day by client; people per week; new people (first session in the period) and returning ones; a table of people with their questions, feedback and last activity, each linking to their sessions. |
| **Grounding** | Of the answered questions: how many used a governed metric (`query_metrics`), only raw SQL (`run_sql`), or no query; how many read context before answering; the metrics queried and the tables answers named; published items no agent was served in the period (candidates for review: stale, or worded so search can't find them). |
| **Performance** | p50 and p95 latency and errors per tool, p95 per day, warehouse queries per connection (errors, truncated results, average rows), and the slowest calls. |
| **Adherence** | How often agents followed Satchel's workflow, rule by rule and by client, with the latest questions that missed a rule. |

## Adherence rules

The MCP server tells every agent how to work (see [the MCP tools](../reference/mcp-tools.md)).
Adherence checks the tool calls behind each question against it:

| Rule | Applies to | Followed when |
|---|---|---|
| Started the session with `start_analysis` | each session started in the period | its first call was `start_analysis` |
| Read context before querying data | questions that ran `run_sql` or `query_metrics` | `get_context_pack` or `search_context` ran earlier in the session (follow-ups reuse it) |
| Checked governed metrics before writing SQL | questions that ran `run_sql` | `list_metrics`, `describe_metric`, `query_metrics` or `get_context_pack` ran before the first `run_sql` |
| Finished the question with `report_answer` | questions with any call besides `start_analysis` | `report_answer` was called for that question |
| Passed the session and question on every call | every tool call | the call could be placed in a question |

The **adherence rate** is the share of questions that followed every rule that applied to them.
A question still in progress counts as not having reported yet.

Whether answers follow the workspace's own [agent instructions](../reference/mcp-tools.md#agent-instructions)
(tone, caveats, which metrics to prefer) is not measured yet: that needs a model to grade the
answers.

## Privacy

The page is for curators and admins (`GET /v1/observability/analytics?days=7|30|90`). People are
named as they are in Sessions. Question text and feedback comments are shortened to 120
characters for everyone but admins, and left out entirely for sessions marked sensitive.
