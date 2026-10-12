#!/usr/bin/env bash
# bridge-gate lease watchdog. Decides NOTHING and writes NOTHING itself: it only finds open pull requests whose gate state records an
# in-flight attempt whose lease has expired and asks the gate to re-evaluate exactly those (workflow_dispatch of bridge-gate.yml, the
# same thing a person does by hand). The gate then records HANDOFF_TIMEOUT, removes the label and notifies, or does nothing if the
# attempt finished meanwhile. At most MAX_DISPATCH pull requests per run. Reads are GET only; the one write is the dispatch.
#
# Env: REPO, DEFAULT_BRANCH, MAX_DISPATCH (default 5), SCAN_LIMIT (default 100 open PRs), NOW, GH, CONFIG (test hooks).
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
GH=${GH:-gh}
CFG=${CONFIG:-$here/config.json}
now=${NOW:-$(date +%s)}
max=${MAX_DISPATCH:-5}
scan=${SCAN_LIMIT:-100}
[[ ${REPO:-} =~ ^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$ && ${DEFAULT_BRANCH:-} =~ ^[A-Za-z0-9._/-]{1,100}$ && ! ${DEFAULT_BRANCH:-} =~ \.\. &&
  $now =~ ^[0-9]+$ && $max =~ ^[0-9]+$ && $scan =~ ^[0-9]+$ ]] || { echo "watchdog: bad input" >&2; exit 1; }
author=$(jq -r .state_author "$CFG") && marker=$(jq -r .state_marker "$CFG") || exit 1

prs=$("$GH" api --paginate "repos/$REPO/pulls?state=open&per_page=100" --jq '.[].number') || { echo "watchdog: cannot list pull requests" >&2; exit 1; }
failed=0; sent=0
for n in $(printf '%s\n' "$prs" | grep -E '^[1-9][0-9]{0,8}$' | head -n "$scan"); do
  ((sent >= max)) && { echo "watchdog: dispatch limit ($max) reached; the rest wait for the next run"; break; }
  comments=$("$GH" api --paginate "repos/$REPO/issues/$n/comments" --jq '.[] | {login: .user.login, type: .user.type, first: ((.body // "") | split("\n")[0])}') ||
    { echo "watchdog: cannot read PR #$n comments; skipped" >&2; failed=1; continue; }
  # Exactly one bot-authored state comment (a person can forge the marker, not the author); an in-flight attempt whose lease has run out.
  expired=$(printf '%s\n' "$comments" | jq -rs --arg a "$author" --arg m "$marker" --argjson now "$now" '
    [.[] | select(.login == $a and .type == "Bot" and (.first | startswith($m))) | .first | ltrimstr($m) | rtrimstr(" -->") | try fromjson catch null]
    | select(length == 1) | .[0] | select(type == "object")
    | [.attempts[]? | select(.status == "in_flight" and (.lease_expires | type) == "number" and .lease_expires <= $now)] | length')
  [[ ${expired:-0} -gt 0 ]] || continue
  echo "watchdog: PR #$n has an expired in-flight lease; asking the gate to re-evaluate it"
  if "$GH" api -X POST "repos/$REPO/actions/workflows/bridge-gate.yml/dispatches" -f "ref=$DEFAULT_BRANCH" -f "inputs[pr_number]=$n" >/dev/null; then sent=$((sent + 1))
  else echo "watchdog: dispatch for PR #$n failed" >&2; failed=1; fi
done
echo "watchdog: dispatched $sent"
exit "$failed"
