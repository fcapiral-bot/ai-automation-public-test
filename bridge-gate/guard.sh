#!/usr/bin/env bash
# Stale-head guard. Refuses a decision that is about to write (state, notice, label removal or label add) when the head it was
# made for is no longer the pull request's head, so a push that lands between evaluation and a write can never record a stale
# decision, post a stale notice or start a correction on a commit that is already gone.
# Exit 0: the decision writes nothing or names no head (evidence errors write nothing), or the head is unchanged.
# Exit 1: stale, or the head cannot be re-read (fail closed). The new head's own event re-evaluates.
set -uo pipefail
GH=${GH:-gh}
f=${1:-decision.json}
want=$(jq -r '.head // empty' "$f") || exit 1
[[ -n $want ]] || exit 0
writes=$(jq -r '((.state_changed == true) or (.notify == true) or (.handoff != null) or (.clear_label == true))' "$f") || exit 1
[[ $writes == true ]] || exit 0
[[ ${REPO:-} =~ ^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$ && ${PR_NUMBER:-} =~ ^[1-9][0-9]{0,8}$ ]] || { echo "guard: bad input; refusing" >&2; exit 1; }
cur=$("$GH" api "repos/$REPO/pulls/$PR_NUMBER" --jq '.head.sha' 2>/dev/null) || { echo "guard: cannot re-read the PR head; refusing" >&2; exit 1; }
[[ $cur == "$want" ]] || { echo "guard: head moved ${want:0:7} -> ${cur:0:7}; refusing a stale decision (the new head's event re-evaluates)" >&2; exit 1; }
