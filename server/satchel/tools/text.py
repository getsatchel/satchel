"""Agent-facing wording. Tested for agent compliance:
rich "when to call" descriptions, a numbered
server ``instructions`` workflow, and corrective errors."""

INSTRUCTIONS = """Satchel is a governed analytics context layer: metric definitions, glossary terms, table docs and guides for this workspace.
Workflow you MUST follow for every data question:
1. Call start_analysis with the user's question to get a session_id.
2. Call get_context_pack (or search_context) BEFORE writing any SQL, to pick up metric definitions, glossary terms and rules. Pass session_id and the user's question.
3. Use list_tables / describe_table for table details.
4. If the query tools (list_metrics, query_metrics, run_sql, get_result) are listed, answer with data: PREFER query_metrics when a governed metric exists (check list_metrics / describe_metric); only use run_sql for questions no metric covers, with a single read-only SELECT that follows the table docs (filters, units, caveats). Use get_result to page through long results. Never paste unverified numbers from memory.
5. If a term the user used has no definition in context, call flag_context_gap.
6. If the user says a number looks wrong, doubts it, corrects you or pushes back, call record_user_feedback.
7. Always finish with report_answer before replying to the user (metrics_used, tables_used, caveats).
Pass session_id and the user's original question (verbatim) on every Satchel tool call. Cite context you used by its [ctx:key@vN] tag."""

WORKFLOW_PROMPT = INSTRUCTIONS

D_START = (
    "Begin an analysis session for a user's data question. Call this FIRST for every data "
    "question, passing the user's question verbatim. Returns a session_id that you must pass "
    "to every later satchel tool call, plus the workspace's always-on rules."
)
D_SEARCH = (
    "Search governed business context (metric definitions, glossary, table docs, guides). "
    "Call this BEFORE writing SQL for any question involving a business term such as revenue, "
    "active customer or refund. REQUIRED: session_id (from start_analysis) and question (the "
    "user's original question, verbatim, not your search query)."
)
D_PACK = (
    "Get the full packed context for a question: workspace rules, caveats, glossary, golden "
    "queries, table docs and guides, ranked and fitted to a token budget. Prefer this over "
    "search_context when starting to answer a question. REQUIRED: session_id and question "
    "(the user's original question, verbatim)."
)
D_ITEM = (
    "Read one context item in full by its key (as shown in a [ctx:key@vN] tag). Use when a "
    "search snippet or pack entry is truncated. REQUIRED: session_id."
)
D_LIST = (
    "List the documented tables available in this workspace, with descriptions. Use after "
    "context search and before describe_table. REQUIRED: session_id."
)
D_DESCRIBE = (
    "Show a table's documentation: columns, enum values, units, grain, caveats and whether it "
    "is endorsed. Use before writing SQL against the table. REQUIRED: session_id and question "
    "(the user's original question, verbatim)."
)
D_REPORT = (
    "REQUIRED final step of every analysis: record the answer you are about to give the user "
    "(summary, metrics used, tables used, confidence, caveats). Call it before replying to the "
    "user, once per answer. Put any assumptions or missing definitions in caveats."
)
D_GAP = (
    "Call this whenever the user's question uses a term, metric or concept that search/pack "
    "did not define (no definition found), is ambiguous, or when data you needed was missing. "
    "Say what was missing and why it mattered. This is how the data team learns what to "
    "document. Do not skip it by only mentioning the gap in report_answer caveats."
)
D_FEEDBACK = (
    "Call this whenever the user expresses doubt, says a number is wrong, corrects you, pushes "
    "back, or gives praise. Capture sentiment (positive | negative | correction | doubt) and "
    "their comment. Call it BEFORE re-working the answer."
)
D_RATE_RESULT = (
    "App-only: the result widget calls this when the user clicks 👍 / 👎 and optionally picks reasons "
    "(wrong_number, wrong_definition, missing_filter, not_what_i_asked) and a comment. "
    "'wrong' is a legacy sentiment meaning 👎 + wrong_number. Not for the model; use record_user_feedback to relay what the user says in chat."
)
D_PROPOSE = (
    "Propose a new or changed context item (definition, guide, caveat...) when you learned "
    "something durable, e.g. the user defined a term. It is saved as a proposal for a curator "
    "to review; it is never published automatically. Requires analyst role."
)

D_LIST_METRICS = (
    "List the governed metrics (cubes, measures, dimensions, time dimensions) defined in this "
    "workspace's semantic model. Call this BEFORE writing SQL for any numeric question "
    "(revenue, order count...): if a metric exists, use query_metrics rather than run_sql so "
    "the number uses the approved definition. REQUIRED: session_id."
)
D_DESCRIBE_METRIC = (
    "Show one governed metric: its expression, the filters baked into its base model, the "
    "dimensions and time grains it supports, and the matching glossary definition. Call it "
    "before query_metrics when unsure how a metric is defined. REQUIRED: session_id and "
    "question (the user's original question, verbatim)."
)
D_QUERY_METRICS = (
    "Run a governed metric query: metrics (e.g. revenue) by optional dimensions, filters, "
    "ordering and limit. PREFER this over run_sql whenever list_metrics shows a metric that "
    "fits (check search_context / get_context_pack first). Time dimensions take a grain, "
    "e.g. dimensions=['order_date:month']; filter dates with gte/lt operators on the time "
    "dimension (end date exclusive). Returns a preview of the rows, the row count, "
    "the generated SQL and a result_id for get_result. REQUIRED: session_id and question "
    "(the user's original question, verbatim)."
)
D_RUN_SQL = (
    "Run ONE read-only SQL SELECT (or WITH ... SELECT) against the workspace's warehouse when "
    "no governed metric fits. Check search_context / get_context_pack and describe_table "
    "first so the SQL uses the documented filters, units and endorsed tables; prefer "
    "query_metrics when a defined metric exists. DDL, DML and multiple statements are "
    "rejected; results are capped. Returns a preview of the rows, the row count, a truncated "
    "flag and a result_id for get_result. REQUIRED: session_id and question (the user's "
    "original question, verbatim)."
)
D_GET_RESULT = (
    "Fetch a page of a previous run_sql / query_metrics result by result_id (offset, limit). "
    "Use it when the preview was truncated or you need more rows. REQUIRED: session_id."
)

ERR_NO_SESSION = (
    "Missing required argument session_id. Call start_analysis(question=<the user's question, "
    "verbatim>) first; it returns a session_id. Then pass that session_id (and question) on "
    "every Satchel tool call."
)
ERR_BAD_SESSION = (
    "Unknown session_id {sid!r} (it must be the value returned by start_analysis). Call "
    "start_analysis(question=<the user's question, verbatim>) to get a valid session_id, then "
    "retry this call."
)
ERR_NO_QUESTION = (
    "Missing required argument question. Pass the user's original question verbatim (not your "
    "search query) so Satchel can attribute this call, then retry."
)
