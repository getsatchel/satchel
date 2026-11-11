# Evals

An eval suite is a set of questions with graders. You run it against a release to measure whether
your context helps agents answer correctly, and against a preview to see whether a change helps or
hurts before you publish it. Suites can also run on a schedule, so you notice when answers drift
because the warehouse data, the model or the context changed.

## A suite

Suites are YAML. Put them in your context repo under `evals/` (imported on every sync, and then
managed by git), or paste one in **Evals → New suite**.

```yaml
id: core                       # letters, digits, _ - . ; the suite's key
name: Core metrics
model: { provider: anthropic, name: claude-sonnet-5-5 }   # optional: pin the reference agent's model
schedule: { interval_hours: 24 }                          # optional: run it every day
cases:
  - id: revenue-march-2025
    tags: [baseline]
    prompt: What was revenue in March 2025?
    attempts: 2                # run the agent twice, up to 5
    rubrics:
      - grader: numeric_value
        expected_sql: select sum(amount) / 100.0 from analytics.orders where ...
        tolerance_pct: 0.5
      - grader: judge_session
        criteria: Excluded returned orders and test orders.
      - grader: context_used
        items: [glossary/business-terms.yaml#revenue]
        warn_only: true        # reported, never fails the case
```

A suite has at most 100 cases, and a case at most 15 rubrics. Each case runs the reference agent:
a tool-use loop that calls Satchel's MCP tools in-process, pinned to the release under test. Eval
sessions are tagged and kept out of observability, signals and curation.

| Grader | Passes when | Needs an LLM |
| --- | --- | --- |
| `numeric_value` | The answer's headline number matches `expected`, or the result of `expected_sql`, within `tolerance_pct` or `tolerance_abs`. | Yes (agent) |
| `sql_equivalent` | The agent's last data query returns the same rows as `expected_sql` (`ordered: true` to compare order). | Yes (agent) |
| `context_used` | Retrieval for the prompt returns the `items` (context item keys), within the top `top_n` when set. | No |
| `judge_final_answer` | An LLM judge says the final answer meets `criteria`. | Yes |
| `judge_session` | An LLM judge says the whole session (tool calls, SQL) meets `criteria`. | Yes |

Each rubric counts towards a `measure` (by default `correctness`, `retrieval`, `answer_quality` or
`method`; set `measure:` to group them your own way). A run reports a pass rate per measure. A
suite made only of `context_used` rubrics runs without any LLM, so it is a cheap retrieval
regression check.

The model comes from, in order: the run's model override, the suite's `model`, then the
workspace's default model (Settings → Models, or `SATCHEL_CHAT_*`). With no model, agent-driven
cases are `skipped`.

## Running a suite

- **UI:** Evals → Suites → **Run**, then pick a published release or a preview.
- **CLI:** `satchel eval run core --workspace acme [--preview <id>] [--model claude-haiku-4-5]`.
  `satchel eval compare <run-a> <run-b> --workspace acme` compares two runs.
- **API:** `POST /v1/evals/runs` with `suite_key` and `release_id`.

Analysts and above run evals; curators and above add suites. Run history lists every run with
what started it: by hand, the publish gate, or the schedule. Tick two completed runs to compare
pass rates per measure and see which cases started or stopped passing.

## Scheduled runs

Add `schedule: { interval_hours: N }` (1 to 168) to a suite and `satchel worker` runs it against
the workspace's latest published release every N hours. The Suites tab shows the schedule and when
the next run is due.

- **When.** The worker checks every `SATCHEL_EVAL_TICK_MIN` minutes (default 15; `0` turns
  scheduled evals off on that worker) on its own thread, so a long eval never delays curation or
  source syncs. A suite is due when its last scheduled run started N hours ago, or it has never
  run on a schedule. Runs you start by hand and gate runs do not move the schedule.
- **Who it runs as.** The reference agent's sessions belong to a user: the user who saved the
  suite's latest version if they are still an analyst or above, else the workspace's oldest
  admin, curator or analyst. The run itself has no starter and is labelled **scheduled**, and its
  `eval.run.start` audit event has no actor and `trigger: schedule`.
- **Comparison.** Each scheduled run remembers the previous completed scheduled run of the suite.
  **Compare with previous** on the run page shows what changed since then.
- **No model.** A suite that needs an LLM is not run while no model is configured; the worker logs
  a warning instead of filling the history with skipped runs.
- **`--once`.** `satchel worker --once` (a CronJob or Cloud Run Job) runs the suites that are due
  after the curation pass, in the same process, so give the job a timeout long enough for them.
  Its schedule is the shortest interval it can honour.
- **Replicas.** Each suite's run is started under a Postgres advisory lock, so several workers
  never start the same scheduled run twice.
- **Cost.** Every scheduled run of an agent-driven suite calls the model for every case and
  attempt. Start with a long interval, or schedule a `context_used`-only suite, which costs
  nothing.

Changing the schedule changes the suite, so it saves a new version. A scheduled run on the
current release can also serve as the publish gate's baseline (see below), when it is less than a
day old and of the same suite version.

## The publish gate

Set `publish.require_evals` in `satchel.config.yaml` (or the workspace settings) and a preview
cannot be published while one of those suites regresses:

```yaml
publish:
  require_evals: [retrieval]     # suite keys
  eval_threshold: 0              # allowed drop in pass rate, in percentage points
```

**Run evals** in Changes runs each required suite on the preview and on its parent release (a
parent run from the last 24 hours is reused), then compares them per measure. In CI,
`satchel eval gate <preview-id> --workspace acme` does the same and exits 1 unless the gate passes.
For a context repo on GitHub, the [GitHub Action](../integrations/github-action.md) builds the
preview from each pull request, runs the gate and comments the comparison.
An admin can publish over a failing gate; that is recorded as `release.publish_override`.
