#!/usr/bin/env bash
# Stale-head guard. Refuses a handoff whose head is no longer the pull request's head, so a push that lands
# between evaluation and handoff can never start a correction on a commit that is already gone.
# Exit 0: nothing to hand off, or the head is unchanged. Exit 1: stale, or the head cannot be re-read (fail closed).
set -uo pipefail
GH=${GH:-gh}
want=$(jq -r '.handoff.head // empty' "${1:-decision.json}") || exit 1
[[ -n $want ]] || exit 0
[[ ${REPO:-} =~ ^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$ && ${PR_NUMBER:-} =~ ^[1-9][0-9]{0,8}$ ]] || { echo "guard: bad input; refusing" >&2; exit 1; }
cur=$("$GH" api "repos/$REPO/pulls/$PR_NUMBER" --jq '.head.sha' 2>/dev/null) || { echo "guard: cannot re-read the PR head; refusing" >&2; exit 1; }
[[ $cur == "$want" ]] || { echo "guard: head moved ${want:0:7} -> ${cur:0:7}; refusing a stale handoff (the new head's event re-evaluates)" >&2; exit 1; }
