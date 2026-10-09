# bridge-gate decision function. PURE: normalized facts + config in, decision + next state out.
# No network, no clock (the caller passes `now`). Run:
#   jq -n --slurpfile facts facts.json --slurpfile cfg config.json -f gate.jq
#
# Decisions: WAIT, NEEDS_FIX, READY, BLOCKED, MAX_ATTEMPTS, USAGE_STOP.
# Every doubt resolves to BLOCKED (no handoff). READY is advisory: nothing here merges.
# Synthetic test facts carry a `_fixture` key and are REJECTED unless `--arg allow_fixture test-fixture-run` is passed;
# no production path passes it and collect.sh never emits the key.
$facts[0] as $f | $cfg[0] as $c
| (if ($f.pr | type) == "object" then $f.pr.head_sha else null end) as $head
| ($f.state // {v: 1, attempts: [], seen: [], last: null}) as $st

| def hex40: type == "string" and test("^[0-9a-f]{40}$");
  def isarr: type == "array";
  def codex($u): $u.login == $c.codex.login and $u.uid == $c.codex.id and $u.type == $c.codex.type;

  # ---- evidence: anything missing, malformed or contradictory blocks everything ----
  def problems:
    [ (($f.incomplete // []) as $i | if ($i | isarr) then $i[] else "incomplete_malformed" end),
      (if ($f.pr | type) != "object" or (($f.pr.head_sha | hex40) | not) or ($f.pr.number | type) != "number"
       then "pr_malformed" else empty end),
      (("reviews", "review_comments", "issue_comments", "check_runs", "own_suite_ids") as $k
       | if ($f[$k] | isarr | not) then $k + "_malformed" else empty end),
      (if ($f.combined_status | type) != "object" then "status_malformed"
       elif $f.combined_status.sha != $head then "status_wrong_commit"
       elif $f.combined_status.listed != $f.combined_status.total then "status_truncated"
       elif ($f.combined_status.state | IN("success", "failure", "error", "pending") | not) then "status_unrecognised"
       else empty end),
      (if $f.state == null then empty
       elif ($f.state | type) == "object" and $f.state.v == 1 and ($f.state.attempts | isarr) and ($f.state.seen | isarr)
       then empty else "state_malformed" end),
      (if ($f | has("_fixture")) and (($ARGS.named.allow_fixture // "") != "test-fixture-run") then "fixture_rejected" else empty end),
      (if ($f.now | type) != "number" then "now_malformed" else empty end),
      (if ($f.head_commit | type) != "object" or (($f.head_commit.subject | type) != "string") or (($f.head_commit.parents | isarr) | not)
       then "head_commit_malformed" else empty end),
      (if ($f.check_runs | isarr) then
         ($f.check_runs[]
          | select((type != "object") or ((.status | type) != "string")
                   or (.status == "completed"
                       and (.conclusion | IN("success", "neutral", "skipped", "failure", "cancelled", "timed_out",
                                             "action_required", "startup_failure", "stale") | not)))
          | "check_run_unrecognised")
       else empty end)
    ] | unique;

  # ---- Codex evidence: only the verified identity (login + id + type), only the CURRENT head ----
  def codex_revs: [$f.reviews[] | select(codex(.))];
  def head_revs: [codex_revs[] | select(.commit_id == $head)];
  def roots($rid): [$f.review_comments[] | select(.in_reply_to == null and .review_id == $rid)] | length;
  def has_findings: roots(.id) > 0 or .state == "CHANGES_REQUESTED";
  def n_findings: [head_revs[] | select(has_findings)] | length;
  def n_unclassified: [head_revs[] | select(has_findings | not)] | length;
  def n_stale: [codex_revs[] | select(.commit_id != $head)] | length;
  def human_blocked:
    [$f.reviews[] | select((codex(.) | not) and (.state | IN("APPROVED", "CHANGES_REQUESTED", "DISMISSED")))]
    | group_by(.login) | map(max_by(.id)) | map(select(.state == "CHANGES_REQUESTED")) | length > 0;
  def summary_lines:
    [$f.issue_comments[]
     | select(codex(.) and ((.body | type) == "string") and (.body | contains($c.summary_marker)))
     | .body | split("\n")[] | select(contains("`" + $head[0:7] + "`"))];

  # ---- CI: independent checks only; this workflow's own check suites are excluded by numeric id ----
  def ci_runs: [$f.check_runs[] | . as $r | select(($f.own_suite_ids | index($r.suite_id)) == null)];
  def ci_fail:
    ([ci_runs[] | select(.status == "completed"
        and (.conclusion | IN("failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale")))] | length)
    + (if $f.combined_status.state | IN("failure", "error") then 1 else 0 end);
  def ci_pend:
    ([ci_runs[] | select(.status != "completed")] | length)
    + (if $f.combined_status.state == "pending" and $f.combined_status.total > 0 then 1 else 0 end);
  # READY needs the DESIGNATED check (name and producing app from config) to have succeeded. Unrelated passing checks and
  # legacy commit statuses never count; failing or pending checks of any kind still do (ci_fail, ci_pend).
  def ci_ok:
    [ci_runs[] | select(.name == $c.ci_check.name and .app_id == $c.ci_check.app_id
                        and .status == "completed" and .conclusion == "success")] | length;
  # Real mode with an activation lock that is not CONFIRMED open: no handoff can start, so none may be recorded.
  def lock_blocked: $f.handoff_mode == "label" and $f.handoff_lock_open != true;

  # ---- persistent per-PR / per-head attempt state ----
  # An in-flight attempt is finished ONLY by evidence that its own session pushed: the current head is a single
  # commit directly on top of the attempt's head, titled exactly as the session is told to title it. Any other new
  # head (a person's push, a rebase, a merge) does NOT end the attempt: it stays in flight until its lease
  # expires, so a second correction session can never start beside a possibly still-running first one.
  def fix_title($a):
    $c.fix_commit_title | sub("\\{n\\}"; ($a.n | tostring)) | sub("\\{max\\}"; ($c.max_attempts | tostring));
  def finished($a): $f.head_commit.subject == fix_title($a) and $f.head_commit.parents == [$a.head];
  def reconciled:
    $st.attempts | map(. as $a | if .status == "in_flight" and .head != $head and finished($a)
                                 then .status = "superseded" | .next_head = $head else . end);

  problems as $p
| if ($p | length) > 0 then
    # Untrusted evidence: touch nothing, hand off nothing, write nothing.
    {decision: "BLOCKED", reason: "EVIDENCE_UNAVAILABLE", problems: $p, duplicate: false, handoff: null,
     notify: false, clear_label: false, state: $f.state, state_changed: false}
  else
    reconciled as $rec
  | ([$rec[] | select(.head == $head)] | last) as $cur
  | ([$rec[] | select(.status == "in_flight")] | last) as $inflight
  | ($f.event.key // null) as $key
  | ($key != null and (($st.seen | index($key)) != null)) as $dup
  | n_findings as $nf | n_unclassified as $nu
  | (if $f.pr.state != "open" or $f.pr.merged == true then {decision: "BLOCKED", reason: "PR_NOT_OPEN"}
     elif $f.pr.head_repo != $f.pr.base_repo then {decision: "BLOCKED", reason: "FORK_PR"}
     elif $f.kill_switch == true then {decision: "USAGE_STOP", reason: "KILL_SWITCH"}
     elif (($f.pr.labels // []) | index($c.labels.usage_stop)) != null then {decision: "USAGE_STOP", reason: "USAGE_STOP_LABEL"}
     elif $f.pr.draft == true then {decision: "WAIT", reason: "DRAFT"}
     elif $inflight != null then
       (if $f.now < $inflight.lease_expires then {decision: "WAIT", reason: "HANDOFF_IN_FLIGHT"}
        else {decision: "BLOCKED", reason: "HANDOFF_TIMEOUT", timeout: true} end)
     elif $cur != null and $cur.status == "timed_out" then {decision: "BLOCKED", reason: "HANDOFF_TIMEOUT"}
     elif human_blocked then {decision: "BLOCKED", reason: "HUMAN_CHANGES_REQUESTED"}
     elif $nu > 0 then {decision: "BLOCKED", reason: "CODEX_REVIEW_UNCLASSIFIED"}
     elif $nf > 0 or ci_fail > 0 then
       (if ($rec | length) >= $c.max_attempts then {decision: "MAX_ATTEMPTS", reason: "ATTEMPT_LIMIT"}
        elif lock_blocked then {decision: "BLOCKED", reason: "HANDOFF_LOCKED"}
        else {decision: "NEEDS_FIX", handoff: true,
              reason: ([(if $nf > 0 then "CODEX_FINDINGS" else empty end), (if ci_fail > 0 then "CI_FAILING" else empty end)] | join("+"))} end)
     elif ci_pend > 0 then {decision: "WAIT", reason: "CI_PENDING"}
     elif (summary_lines | length) == 0 then {decision: "WAIT", reason: "AWAITING_CODEX_REVIEW"}
     elif (any(summary_lines[]; contains("**Completed**")) | not) then {decision: "WAIT", reason: "CODEX_IN_PROGRESS"}
     elif $c.require_ci and ci_ok == 0 then {decision: "WAIT", reason: "CI_MISSING"}
     else {decision: "READY", reason: "CODEX_CLEAN_AND_CI_PASSING"} end) as $d
  | (if $d.timeout == true
     then $rec | map(if .status == "in_flight" then .status = "timed_out" else . end)
     else $rec end) as $recT
  | (if $d.handoff == true
     then $recT + [{n: (($recT | length) + 1), head: $head, trigger: $key, reason: $d.reason, at: $f.now,
                    lease_expires: ($f.now + $c.lease_minutes * 60), status: "in_flight"}]
     else $recT end) as $atts
  | (($st.seen + (if $key != null and ($dup | not) then [$key] else [] end)) | .[-20:]) as $seen
  | {v: 1, attempts: $atts, seen: $seen, last: {decision: $d.decision, reason: $d.reason, head: $head}} as $new
  | {decision: $d.decision, reason: $d.reason, head: $head, duplicate: $dup, fixture: ($f._fixture // null),
     handoff: (if $d.handoff == true
               then {pr: $f.pr.number, head: $head, attempt: ($atts | length), of: $c.max_attempts, reason: $d.reason,
                     label: $c.labels.needs_fix, mode: ($f.handoff_mode // "simulate"), lease_expires: ($atts | last | .lease_expires)}
               else null end),
     notify: ((($d.decision | IN("READY", "BLOCKED", "MAX_ATTEMPTS", "USAGE_STOP")))
              and ($st.last == null or $st.last.decision != $d.decision or $st.last.reason != $d.reason or $st.last.head != $head)),
     clear_label: ($d.timeout == true or ([$rec[] | select(.status == "superseded")] | length) != ([$st.attempts[] | select(.status == "superseded")] | length)),
     state: $new, state_changed: ($new != $f.state),
     evidence: {codex_findings: $nf, codex_unclassified: $nu, codex_stale: n_stale, ci_failing: ci_fail,
                ci_pending: ci_pend, ci_passing: ci_ok, attempts_used: ($atts | length)}}
  end
