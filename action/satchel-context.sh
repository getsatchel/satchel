#!/usr/bin/env bash
# The Satchel context GitHub Action (action.yml). Needs bash, curl, jq, tar and gh, which GitHub's
# hosted runners have. It can also run locally: set the variables action.yml sets.
#
#   preview: upload the folder (POST /v1/sync/preview), run the gate (POST/GET
#            /v1/previews/{id}/evals), comment on the pull request, fail if the gate fails.
#   publish: upload the folder (POST /v1/sync/import), which imports and publishes it.
set -euo pipefail

: "${SATCHEL_URL:?url is required}" "${SATCHEL_TOKEN:?token is required}"
[ -n "${GITHUB_ACTIONS:-}" ] && echo "::add-mask::$SATCHEL_TOKEN"
URL="${SATCHEL_URL%/}"
CONTEXT_PATH="${CONTEXT_PATH:-.}"
MODE="${MODE:-auto}"
OUT="${GITHUB_OUTPUT:-/dev/null}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"
MARKER="<!-- satchel-context-action -->"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [ "$MODE" = auto ]; then
  case "${GITHUB_EVENT_NAME:-}" in
    pull_request|pull_request_target) MODE=preview ;;
    *) MODE=publish ;;
  esac
fi
[ -f "$CONTEXT_PATH/satchel.config.yaml" ] \
  || { echo "::error::$CONTEXT_PATH/satchel.config.yaml not found: set the path input"; exit 1; }

sha="${PR_HEAD_SHA:-${GITHUB_SHA:-local}}"
ref="${GITHUB_REF:-local}@${sha:0:7}"
# COPYFILE_DISABLE: macOS tar would add ._* metadata files.
COPYFILE_DISABLE=1 tar -czf "$WORK/bundle.tgz" --exclude=.git --exclude='._*' -C "$CONTEXT_PATH" .

# api METHOD PATH [curl args...]: the response body is left in $WORK/body. Exits with the
# server's message on an HTTP error. (Not run in $(...), so the exit ends the script.)
api() {
  local method=$1 path=$2 code
  shift 2
  code=$(curl -sS --max-time 900 -o "$WORK/body" -w '%{http_code}' -X "$method" \
    -H "Authorization: Bearer $SATCHEL_TOKEN" "$@" "$URL$path") || {
    echo "::error::could not reach $URL$path"; exit 1; }
  if [ "$code" -ge 300 ]; then
    echo "::error::$method $path returned HTTP $code: $(jq -c '.detail // .' "$WORK/body" 2>/dev/null \
      || cat "$WORK/body")"
    exit 1
  fi
}

if [ "$MODE" = publish ]; then
  api POST /v1/sync/import -F "bundle=@$WORK/bundle.tgz" -F "ref=$ref"
  rep=$(cat "$WORK/body")
  seq=$(jq -r '.release_seq // empty' <<<"$rep")
  echo "release-seq=$seq" >>"$OUT"
  {
    echo "### Satchel: imported \`${CONTEXT_PATH}\`"
    jq -r '"Release #\(.release_seq // "none")\(if .release_created then " published" else " (unchanged)" end): \(.created | length) created, \(.updated | length) updated, \(.deprecated | length) deprecated, \(.unchanged) unchanged."' <<<"$rep"
  } | tee -a "$SUMMARY"
  exit 0
fi

note="PR #${PR_NUMBER:-?}: ${PR_TITLE:-}"
api POST /v1/sync/preview -F "bundle=@$WORK/bundle.tgz" -F "ref=$ref" -F "note=${note:0:200}"
rep=$(cat "$WORK/body")
pid=$(jq -r '.preview_id // empty' <<<"$rep")
echo "preview-id=$pid" >>"$OUT"

gate='{"status":"none","suites":[]}'
if [ -n "$pid" ] && [ "${RUN_EVALS:-true}" = true ] && [ "$(jq '.gate_required | length' <<<"$rep")" -gt 0 ]; then
  api POST "/v1/previews/$pid/evals" -H 'Content-Type: application/json' -d '{}'
  gate=$(cat "$WORK/body")
  deadline=$(( $(date +%s) + ${WAIT_MINUTES:-30} * 60 ))
  while [ "$(jq -r .status <<<"$gate")" = running ] && [ "$(date +%s)" -lt "$deadline" ]; do
    sleep 15
    api GET "/v1/previews/$pid/evals"
    gate=$(cat "$WORK/body")
  done
fi
status=$(jq -r .status <<<"$gate")
echo "gate-status=$status" >>"$OUT"

body="$WORK/comment.md"
{
  echo "$MARKER"
  echo "### Satchel context preview"
  if [ -z "$pid" ]; then
    echo "This pull request changes no context: \`${CONTEXT_PATH}\` matches the published release."
  else
    jq -r --arg url "$URL" '"[Preview #\(.preview_seq)](\($url)/changes?preview=\(.preview_id)) of release #\(.base_release_seq // "none"): \(.created | length) new, \(.updated | length) changed, \(.removed | length) removed."' <<<"$rep"
    echo
    jq -r '
      def list(name; xs): if (xs | length) > 0 then
        "<details><summary>\(name) (\(xs | length))</summary>\n\n" +
        (xs[:50] | map("- `\(.)`") | join("\n")) +
        (if (xs | length) > 50 then "\n- …" else "" end) + "\n</details>\n" else empty end;
      list("New"; .created), list("Changed"; .updated), list("Removed"; .removed)' <<<"$rep"
  fi
  case "$status" in
    none) [ -n "$pid" ] && [ "$(jq '.gate_required | length' <<<"$rep")" -eq 0 ] \
            && echo "No publish gate is configured (\`publish.require_evals\`)." ;;
    pass) echo "**Eval gate: passed.**" ;;
    fail) echo "**Eval gate: failed.** A required measure fell more than the threshold." ;;
    running) echo "**Eval gate: still running** after ${WAIT_MINUTES:-30} minutes; see the preview." ;;
    *) echo "**Eval gate: $status.**" ;;
  esac
  if [ "$(jq '.suites | length' <<<"$gate")" -gt 0 ]; then
    echo
    echo "| Suite | Status | Measure | Published | Preview | Change |"
    echo "|---|---|---|---|---|---|"
    jq -r '
      def p: if . == null then "n/a" else ((. * 100 | round | tostring) + "%") end;
      def d: if . == null then "" else ((. * 100 | round) as $x
        | (if $x > 0 then "+" else "" end) + ($x | tostring) + " pts") end;
      .suites[] as $s
      | if ($s.measures | length) == 0 then
          "| `\($s.suite_key)` v\($s.suite_version) | \($s.status) | | | | \($s.message) |"
        else $s.measures[] as $m
          | ([$s.regressions[].measure] | index($m.measure)) as $bad
          | "| `\($s.suite_key)` v\($s.suite_version) | \($s.status) | \($m.measure) | \($m.a | p) | \($m.b | p) | \($m.delta | d)\(if $bad != null then " ⚠️" else "" end) |"
        end' <<<"$gate"
    jq -r '[.suites[].newly_failing[] | "`\(.case)`"] | select(length > 0)
      | "\nNewly failing: " + join(", ")' <<<"$gate"
  fi
  suites=$(jq -r '.eval_suites_changed | join(", ")' <<<"$rep")
  [ -n "$suites" ] && echo -e "\nThis pull request changes eval suites ($suites). The gate uses the suites on the server until it merges."
  if [ "$(jq '.warnings | length' <<<"$rep")" -gt 0 ]; then
    echo
    jq -r '.warnings[:20][] | "> ⚠️ \(.)\n>"' <<<"$rep"
  fi
  echo
  echo "<sub>Published when this pull request merges.</sub>"
} >"$body"
cat "$body" >>"$SUMMARY"

if [ "${COMMENT:-true}" = true ] && [ -n "${PR_NUMBER:-}" ] && [ -n "${GH_TOKEN:-}" ]; then
  repo="${GITHUB_REPOSITORY:?}"
  existing=$(gh api "repos/$repo/issues/$PR_NUMBER/comments" --paginate \
    -q ".[] | select(.body | startswith(\"$MARKER\")) | .id" | head -n1 || true)
  if [ -n "$existing" ]; then
    gh api -X PATCH "repos/$repo/issues/comments/$existing" -F "body=@$body" >/dev/null
  else
    gh api "repos/$repo/issues/$PR_NUMBER/comments" -F "body=@$body" >/dev/null
  fi
fi

if [ "${FAIL_ON_GATE:-true}" = true ] && [ -n "$pid" ] && [ "$status" != none ] && [ "$status" != pass ]; then
  echo "::error::the Satchel eval gate is $status"
  exit 1
fi
