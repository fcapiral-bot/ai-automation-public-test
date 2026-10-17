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
err=$(mktemp); wd=$(mktemp -d); trap 'rm -rf "$err" "$wd"' EXIT

# The attempt was recorded BEFORE this script runs (write-ahead). When `add` is refused after that because the scope was left since
# collection (the lock's own race stays the residual recorded in SECURITY.md S11), the label is never applied and no Routine can start, so the attempt must not wait out its lease and use up one of the three.
# withdraw takes back ONLY the attempt this very run recorded (the last one, still in_flight, for this head and number): earlier attempts are
# never touched and none is ever deleted for any other reason. If that cannot be done the step fails loudly and the attempt stays recorded.
withdraw() {
  local why=$1 id
  jq -e '.handoff != null and ((.state.attempts | last) as $a | $a.status == "in_flight" and $a.n == .handoff.attempt and $a.head == .handoff.head)' decision.json > /dev/null 2>&1 \
    || { echo "label.sh: REFUSED ($why) but the recorded attempt is not the one this run created; leaving it, failing the step" >&2; exit 1; }
  id=$(jq -r '.state_comment_id // empty' facts.json 2>/dev/null)
  if [[ -z $id ]]; then       # this run created the state comment: find it (the bot's, exactly one)
    id=$("$GH" api --paginate "repos/$REPO/issues/$PR_NUMBER/comments" --jq "[.[] | select(.user.login == \"$(jq -r .state_author "$here/config.json")\" and ((.body // \"\") | startswith(\"$(jq -r .state_marker "$here/config.json")\"))) | .id] | if length == 1 then .[0] else empty end" 2>"$err") || id=""
  fi
  [[ $id =~ ^[0-9]{1,20}$ ]] || { echo "label.sh: REFUSED ($why) but the state comment cannot be found; attempt stays recorded, failing the step" >&2; exit 1; }
  jq --arg why "$why" '.state.attempts |= .[:-1] | .state.last = {decision: "WAIT", reason: $why, head: .head}' decision.json > "$wd/withdrawn.json" \
    && jq -r --arg kind state --slurpfile cfg "$here/config.json" -f "$here/render.jq" "$wd/withdrawn.json" > "$wd/body.txt" \
    && "$GH" api -X PATCH "repos/$REPO/issues/comments/$id" -F body=@"$wd/body.txt" > /dev/null 2>"$err" \
    || { echo "label.sh: REFUSED ($why) but the attempt could not be withdrawn ($(head -c 120 "$err")); it stays recorded, failing the step" >&2; exit 1; }
  echo "REAL handoff refused ($why): no label applied; the attempt this run recorded was withdrawn (it never started)."
  exit 0
}
refuse() { [[ $op == add ]] && withdraw "$2"; echo "$1"; exit 0; }       # only the scope checks below use it
if cfg=$("$GH" api -H 'Accept: application/vnd.github.raw+json' "repos/$REPO/contents/bridge-gate/config.json?ref=$DEFAULT_BRANCH" 2>"$err"); then :
elif grep -q 'HTTP 404' "$err"; then echo "REAL handoff is locked off (no bridge-gate/config.json on the default branch); doing nothing."; exit 0
else echo "label.sh: cannot read the activation lock ($(head -c 120 "$err")); failing so that this run can be repeated" >&2; exit 1; fi
jq -e '.real_handoff_enabled == true' <<<"$cfg" > /dev/null 2>&1 || { echo "REAL handoff is locked off (real_handoff_enabled is not true on the default branch); doing nothing."; exit 0; }
# The scope (`handoff_branch_prefix`, default branch) is checked again against the PR's CURRENT head branch right before the label is added,
# like the lock: a prefix narrowed or a branch renamed since collection means no label. An unreadable branch fails the step (a re-run repeats it).
if [[ $op == add ]]; then
  prefix=$(jq -c '.handoff_branch_prefix' <<<"$cfg" 2>/dev/null) || prefix=false
  if [[ $prefix != null && $prefix != '""' ]]; then
    jq -e 'type == "string"' <<<"$prefix" > /dev/null 2>&1 || refuse "REAL handoff refused: handoff_branch_prefix is not a string; doing nothing." HANDOFF_OUT_OF_SCOPE
    ref=$("$GH" api "repos/$REPO/pulls/$PR_NUMBER" --jq .head.ref 2>"$err") || { echo "label.sh: cannot read the PR's head branch ($(head -c 120 "$err")); failing so that this run can be repeated" >&2; exit 1; }
    jq -en --argjson p "$prefix" --arg r "$ref" '$r | startswith($p)' > /dev/null 2>&1 || refuse "REAL handoff refused: head branch is outside handoff_branch_prefix; doing nothing." HANDOFF_OUT_OF_SCOPE
  fi
fi
label=$(jq -r .labels.needs_fix "$here/config.json"); enc=$(jq -rn --arg l "$label" '$l | @uri')
if [[ $op == remove ]]; then
  if ! "$GH" api -X DELETE "repos/$REPO/issues/$PR_NUMBER/labels/$enc" > /dev/null 2>"$err" && ! grep -q 'HTTP 404' "$err"; then
    echo "label.sh: label removal failed ($(head -c 120 "$err")); failing so that this run can be repeated" >&2; exit 1
  fi
else
  "$here/guard.sh" decision.json || exit 1
  "$GH" api -X POST "repos/$REPO/issues/$PR_NUMBER/labels" -f "labels[]=$label" > /dev/null || { echo "label.sh: adding the label failed" >&2; exit 1; }
fi
