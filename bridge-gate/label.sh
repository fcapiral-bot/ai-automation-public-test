#!/usr/bin/env bash
# The ONLY label writer (real handoff; never runs unless BRIDGE_HANDOFF_MODE=label, see the workflow).
#   label.sh remove  clears a superseded or timed-out handoff label. Idempotent: only HTTP 404 ("already gone") is tolerated; any other
#                    error fails the step, so the run stops BEFORE the state is committed and a re-run repeats the removal.
#   label.sh add     applies the handoff label, after the attempt has been persisted (write-ahead) and after the stale-head guard.
# Both do nothing unless real_handoff_enabled is boolean true in bridge-gate/config.json on the DEFAULT branch. A definitive "closed"
# (false, or no such file: HTTP 404) means nothing to do; an unreadable lock (any other API error) fails the step instead of
# silently skipping the work. Env: REPO, PR_NUMBER, DEFAULT_BRANCH, GH_TOKEN. Reads ./decision.json.
set -uo pipefail
GH=${GH:-gh}
here=$(cd "$(dirname "$0")" && pwd)
op=${1:-}
[[ $op == add || $op == remove ]] || { echo "usage: label.sh add|remove" >&2; exit 2; }
todo=$(jq -r --arg op "$op" 'if $op == "add" then (.handoff != null) else (.clear_label == true) end' decision.json) || exit 1
[[ $todo == true ]] || exit 0                                   # nothing to do: no lock read, no API call
[[ ${REPO:-} =~ ^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$ && ${PR_NUMBER:-} =~ ^[1-9][0-9]{0,8}$ \
   && ${DEFAULT_BRANCH:-} =~ ^[A-Za-z0-9._/-]{1,100}$ && ! ${DEFAULT_BRANCH:-} =~ \.\. ]] || { echo "label.sh: bad input; refusing" >&2; exit 1; }
err=$(mktemp); trap 'rm -f "$err"' EXIT
if cfg=$("$GH" api -H 'Accept: application/vnd.github.raw+json' "repos/$REPO/contents/bridge-gate/config.json?ref=$DEFAULT_BRANCH" 2>"$err"); then :
elif grep -q 'HTTP 404' "$err"; then echo "REAL handoff is locked off (no bridge-gate/config.json on the default branch); doing nothing."; exit 0
else echo "label.sh: cannot read the activation lock ($(head -c 120 "$err")); failing so that this run can be repeated" >&2; exit 1; fi
jq -e '.real_handoff_enabled == true' <<<"$cfg" > /dev/null 2>&1 || { echo "REAL handoff is locked off (real_handoff_enabled is not true on the default branch); doing nothing."; exit 0; }
label=$(jq -r .labels.needs_fix "$here/config.json"); enc=$(jq -rn --arg l "$label" '$l | @uri')
if [[ $op == remove ]]; then
  if ! "$GH" api -X DELETE "repos/$REPO/issues/$PR_NUMBER/labels/$enc" > /dev/null 2>"$err" && ! grep -q 'HTTP 404' "$err"; then
    echo "label.sh: label removal failed ($(head -c 120 "$err")); failing so that this run can be repeated" >&2; exit 1
  fi
else
  "$here/guard.sh" decision.json || exit 1
  "$GH" api -X POST "repos/$REPO/issues/$PR_NUMBER/labels" -f "labels[]=$label" > /dev/null || { echo "label.sh: adding the label failed" >&2; exit 1; }
fi
