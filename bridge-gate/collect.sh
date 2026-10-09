#!/usr/bin/env bash
# bridge-gate evidence collector. READ-ONLY: only `gh api` GET calls. Prints one normalized facts
# JSON on stdout for gate.jq. Every failed or malformed call is recorded in `incomplete`, never
# guessed around, so a flaky API can only ever produce BLOCKED, never a handoff.
#
# Env: REPO, PR_NUMBER (validated), EVENT_NAME, EVENT_KEY, KILL_SWITCH, HANDOFF_MODE,
#      GITHUB_RUN_ID (this run, to exclude the gate's own checks), GH (test hook), NOW, CONFIG.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
GH=${GH:-gh}
CFG=${CONFIG:-$here/config.json}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
inc=()

# api LABEL JQ-FILTER ENDPOINT: every page, filtered, slurped into $tmp/LABEL.json (a JSON array).
api() {
  local label=$1 filter=$2 endpoint=$3 raw
  if raw=$("$GH" api --paginate "$endpoint" --jq "$filter" 2>/dev/null) &&
     printf '%s' "$raw" | jq -s '.' > "$tmp/$label.json" 2>/dev/null; then :
  else inc+=("$label"); echo '[]' > "$tmp/$label.json"; fi
}
# one LABEL JQ-FILTER ENDPOINT: a single object into $tmp/LABEL.json (null on failure).
one() {
  local label=$1 filter=$2 endpoint=$3
  if "$GH" api "$endpoint" --jq "$filter" 2>/dev/null | jq -e . > "$tmp/$label.json" 2>/dev/null; then :
  else inc+=("$label"); echo 'null' > "$tmp/$label.json"; fi
}

if ! [[ ${REPO:-} =~ ^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$ && ${PR_NUMBER:-} =~ ^[1-9][0-9]{0,8}$ ]]; then
  jq -n '{incomplete: ["input"]}'; exit 0
fi
base="repos/$REPO"

one pr '{number, state, merged, draft, head_sha: .head.sha, head_repo: .head.repo.full_name, base_repo: .base.repo.full_name, labels: [.labels[].name]}' "$base/pulls/$PR_NUMBER"
HEAD=$(jq -r '.head_sha // empty' "$tmp/pr.json")
if ! [[ $HEAD =~ ^[0-9a-f]{40}$ ]]; then HEAD=""; inc+=("head"); fi

api reviews '.[] | {id, login: .user.login, uid: .user.id, type: .user.type, state, commit_id}' "$base/pulls/$PR_NUMBER/reviews"
api review_comments '.[] | {review_id: .pull_request_review_id, in_reply_to: .in_reply_to_id}' "$base/pulls/$PR_NUMBER/comments"
api all_comments '.[] | {id, login: .user.login, uid: .user.id, type: .user.type, body: (.body // "")}' "$base/issues/$PR_NUMBER/comments"

echo '[]' > "$tmp/check_runs.json"; echo 'null' > "$tmp/status.json"; echo '[]' > "$tmp/runs.json"; echo 'null' > "$tmp/run.json"; echo 'null' > "$tmp/head_commit.json"
if [[ -n $HEAD ]]; then
  # Completion evidence for an in-flight attempt: the head commit's title and parent (see gate.jq `finished`).
  one head_commit '{subject: (.commit.message | split("\n")[0]), parents: [.parents[].sha]}' "$base/commits/$HEAD"
  api check_runs '.check_runs[] | {name, status, conclusion, suite_id: .check_suite.id}' "$base/commits/$HEAD/check-runs"
  one status '{state, total: .total_count, listed: (.statuses | length), sha}' "$base/commits/$HEAD/status?per_page=100"
  # The gate's own check runs are told apart by NUMERIC workflow id, never by display name.
  if [[ ${GITHUB_RUN_ID:-} =~ ^[0-9]+$ ]]; then
    one run '{workflow_id}' "$base/actions/runs/$GITHUB_RUN_ID"
    api runs '.workflow_runs[] | {workflow_id, suite: .check_suite_id}' "$base/actions/runs?head_sha=$HEAD&per_page=100"
  fi
fi
WID=$(jq -r '.workflow_id // empty' "$tmp/run.json")
if [[ -n ${GITHUB_RUN_ID:-} && ! $WID =~ ^[0-9]+$ ]]; then inc+=("own_workflow"); WID=0; fi

# Persistent state: one bot-authored PR comment. A human can forge the marker but not the author.
jq --arg a "$(jq -r .state_author "$CFG")" --arg m "$(jq -r .state_marker "$CFG")" \
   '[.[] | select(.login == $a and .type == "Bot" and (.body | startswith($m))) | {id, first: (.body | split("\n")[0])}]' \
   "$tmp/all_comments.json" > "$tmp/state_comments.json"
state=null; state_id=null
case $(jq length "$tmp/state_comments.json") in
  0) ;;
  1) line=$(jq -r '.[0].first' "$tmp/state_comments.json")
     json=${line#"$(jq -r .state_marker "$CFG")"}; json=${json% -->}
     if jq -e 'type == "object"' <<<"$json" >/dev/null 2>&1; then state=$json; state_id=$(jq '.[0].id' "$tmp/state_comments.json")
     else inc+=("state_corrupt"); fi ;;
  *) inc+=("state_duplicate") ;;
esac

jq --arg login "$(jq -r .codex.login "$CFG")" \
   '[.[] | select(.login == $login) | .body = .body[0:4000]]' "$tmp/all_comments.json" > "$tmp/issue_comments.json"

mode=${HANDOFF_MODE:-simulate}; [[ $mode == label ]] || mode=simulate
jq -n \
  --argjson now "${NOW:-$(date +%s)}" --arg name "${EVENT_NAME:-}" --arg key "${EVENT_KEY:-}" \
  --argjson kill "$([[ ${KILL_SWITCH:-} == true ]] && echo true || echo false)" --arg mode "$mode" \
  --argjson wid "${WID:-0}" --argjson state "$state" --argjson sid "$state_id" \
  --argjson inc "$(printf '%s\n' ${inc[@]+"${inc[@]}"} | jq -R . | jq -s 'map(select(length > 0)) | unique')" \
  --slurpfile pr "$tmp/pr.json" --slurpfile reviews "$tmp/reviews.json" --slurpfile rc "$tmp/review_comments.json" \
  --slurpfile ic "$tmp/issue_comments.json" --slurpfile cr "$tmp/check_runs.json" --slurpfile st "$tmp/status.json" \
  --slurpfile runs "$tmp/runs.json" --slurpfile hc "$tmp/head_commit.json" '
  {now: $now, event: {name: $name, key: (if $key == "" then null else $key end)}, incomplete: $inc,
   kill_switch: $kill, handoff_mode: $mode, pr: $pr[0], reviews: $reviews[0], review_comments: $rc[0],
   issue_comments: $ic[0], check_runs: $cr[0], combined_status: $st[0],
   head_commit: $hc[0], own_suite_ids: [$runs[0][] | select(.workflow_id == $wid) | .suite],
   state: $state, state_comment_id: $sid}'
