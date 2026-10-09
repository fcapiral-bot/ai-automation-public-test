"""Decision tests for bridge-gate/gate.jq (run through the real jq program, no mocks of the logic).

Covers the six decisions, stale reviews, repeated events, overlapping sessions, the attempt limit,
reviewer identity and fail-closed evidence handling. Nothing here touches GitHub or any AI.
"""

import copy
import shutil
import unittest

from . import gate_sim as g
from .gate_sim import CODEX, HUMAN, LOOKALIKE, check, facts, inline, review, run_gate, sha, summary


def findings_facts(head=None, **over):
    head = head or sha(1)
    f = facts(head, reviews=[review(101, head)], review_comments=[inline(101)], check_runs=[check("unit-tests")])
    f.update(over)
    return f


def clean_facts(head=None, **over):
    head = head or sha(1)
    f = facts(head, issue_comments=[summary(head)], check_runs=[check("unit-tests")])
    f.update(over)
    return f


def session_commit(n, parent, of=3):
    return {"subject": "Address review findings (bridge attempt %d/%d)" % (n, of), "parents": [parent]}


def attempt(n, head, status="superseded", at=g.NOW - 10_000, lease=None):
    return {"n": n, "head": head, "trigger": "review:%d" % n, "reason": "CODEX_FINDINGS", "at": at,
            "lease_expires": lease if lease is not None else at + 5400, "status": status}


def state(*attempts, seen=(), last=None):
    return {"v": 1, "attempts": list(attempts), "seen": list(seen), "last": last}


class JqPresent(unittest.TestCase):
    def test_jq_is_installed(self):
        self.assertIsNotNone(shutil.which("jq"), "bridge-gate needs jq (preinstalled on ubuntu-latest runners)")


class DecisionTableTests(unittest.TestCase):
    def test_quiet_pr_waits_for_codex(self):
        d = run_gate(facts())
        self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("WAIT", "AWAITING_CODEX_REVIEW", None))

    def test_findings_on_current_head_need_a_fix(self):
        d = run_gate(findings_facts())
        self.assertEqual((d["decision"], d["reason"]), ("NEEDS_FIX", "CODEX_FINDINGS"))
        self.assertEqual((d["handoff"]["attempt"], d["handoff"]["of"], d["handoff"]["head"], d["handoff"]["mode"]),
                         (1, 3, sha(1), "simulate"))
        self.assertEqual(d["state"]["attempts"][0]["status"], "in_flight")
        self.assertEqual(d["state"]["attempts"][0]["lease_expires"], g.NOW + 90 * 60)

    def test_failing_ci_needs_a_fix_too(self):
        for label, over in (("check run", {"check_runs": [check("unit-tests", conclusion="failure")]}),
                            ("commit status", {"combined_status": {"state": "failure", "total": 1, "listed": 1, "sha": sha(1)}})):
            with self.subTest(label):
                d = run_gate(facts(**over))
                self.assertEqual((d["decision"], d["reason"]), ("NEEDS_FIX", "CI_FAILING"))

    def test_findings_and_failing_ci_share_one_attempt(self):
        d = run_gate(findings_facts(check_runs=[check("unit-tests", conclusion="failure")]))
        self.assertEqual((d["reason"], len(d["state"]["attempts"])), ("CODEX_FINDINGS+CI_FAILING", 1))

    def test_pending_ci_waits(self):
        for run in (check("unit-tests", "in_progress", None), check("unit-tests", "queued", None)):
            self.assertEqual(run_gate(clean_facts(check_runs=[run]))["reason"], "CI_PENDING")

    def test_clean_codex_and_passing_ci_is_ready_and_never_hands_off(self):
        d = run_gate(clean_facts())
        self.assertEqual((d["decision"], d["handoff"], d["notify"]), ("READY", None, True))

    def test_ready_needs_real_ci_evidence(self):
        self.assertEqual(run_gate(clean_facts(check_runs=[]))["reason"], "CI_MISSING")
        skipped = run_gate(clean_facts(check_runs=[check("unit-tests", conclusion="skipped")]))
        self.assertEqual((skipped["decision"], skipped["reason"]), ("WAIT", "CI_MISSING"))
        cfg = copy.deepcopy(g.CONFIG); cfg["require_ci"] = False
        self.assertEqual(run_gate(clean_facts(check_runs=[]), cfg)["decision"], "READY")

    def test_a_green_commit_status_alone_does_not_count_as_ci(self):
        # Codex finding on PR #1: only the designated check (name + GitHub Actions app) can satisfy require_ci.
        d = run_gate(clean_facts(check_runs=[], combined_status={"state": "success", "total": 2, "listed": 2, "sha": sha(1)}))
        self.assertEqual((d["decision"], d["reason"]), ("WAIT", "CI_MISSING"))

    def test_codex_not_finished_is_not_ready(self):
        h = sha(1)
        self.assertEqual(run_gate(facts(h, issue_comments=[summary(h, completed=False)], check_runs=[check("unit-tests")]))["reason"],
                         "CODEX_IN_PROGRESS")

    def test_absence_of_findings_is_not_a_pass(self):
        # No Codex evidence at all, even with green CI, can never be READY.
        d = run_gate(facts(check_runs=[check("unit-tests")]))
        self.assertEqual((d["decision"], d["reason"]), ("WAIT", "AWAITING_CODEX_REVIEW"))

    def test_draft_waits_even_with_findings(self):
        f = findings_facts(); f["pr"]["draft"] = True
        d = run_gate(f)
        self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("WAIT", "DRAFT", None))

    def test_closed_merged_and_fork_prs_are_blocked(self):
        for label, patch, reason in (("closed", {"state": "closed"}, "PR_NOT_OPEN"), ("merged", {"merged": True}, "PR_NOT_OPEN"),
                                     ("fork", {"head_repo": "evil/r"}, "FORK_PR")):
            with self.subTest(label):
                f = findings_facts(); f["pr"].update(patch)
                d = run_gate(f)
                self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("BLOCKED", reason, None))

    def test_usage_stop_by_kill_switch_or_label_beats_a_pending_fix(self):
        d = run_gate(findings_facts(kill_switch=True))
        self.assertEqual((d["decision"], d["reason"], d["handoff"], d["notify"]), ("USAGE_STOP", "KILL_SWITCH", None, True))
        f = findings_facts(); f["pr"]["labels"] = ["bridge:usage-stop"]
        self.assertEqual(run_gate(f)["reason"], "USAGE_STOP_LABEL")

    def test_human_changes_requested_blocks_until_that_reviewer_moves_on(self):
        def blocked(reviews):
            return run_gate(clean_facts(reviews=reviews))["reason"]
        cr = review(5, sha(1), HUMAN, "CHANGES_REQUESTED")
        self.assertEqual(blocked([cr]), "HUMAN_CHANGES_REQUESTED")
        self.assertNotEqual(blocked([cr, review(6, sha(1), HUMAN, "APPROVED")]), "HUMAN_CHANGES_REQUESTED")
        other = {"login": "someone-else", "uid": 1, "type": "User"}
        self.assertEqual(blocked([cr, review(6, sha(1), other, "APPROVED")]), "HUMAN_CHANGES_REQUESTED")
        self.assertNotEqual(blocked([review(5, sha(1), HUMAN, "DISMISSED")]), "HUMAN_CHANGES_REQUESTED")
        self.assertEqual(blocked([review(7, sha(1), HUMAN, "APPROVED"), review(8, sha(1), HUMAN, "CHANGES_REQUESTED")]),
                         "HUMAN_CHANGES_REQUESTED")

    def test_codex_review_we_cannot_classify_blocks_instead_of_passing(self):
        h = sha(1)
        d = run_gate(facts(h, reviews=[review(101, h)], issue_comments=[summary(h)], check_runs=[check("unit-tests")]))
        self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("BLOCKED", "CODEX_REVIEW_UNCLASSIFIED", None))
        # a reply is not a finding either
        d = run_gate(facts(h, reviews=[review(101, h)], review_comments=[inline(101, reply_to=55)]))
        self.assertEqual(d["reason"], "CODEX_REVIEW_UNCLASSIFIED")

    def test_codex_changes_requested_review_is_a_finding(self):
        h = sha(1)
        d = run_gate(facts(h, reviews=[review(101, h, CODEX, "CHANGES_REQUESTED")]))
        self.assertEqual(d["decision"], "NEEDS_FIX")

    def test_the_gates_own_check_runs_are_excluded_by_numeric_suite_not_by_name(self):
        own = check("bridge-gate", "in_progress", None, suite=500)
        same_name_elsewhere = check("bridge-gate", "completed", "failure", suite=100)
        d = run_gate(clean_facts(check_runs=[check("unit-tests"), own], own_suite_ids=[500]))
        self.assertEqual(d["decision"], "READY")  # own in-progress run is not pending CI
        d = run_gate(clean_facts(check_runs=[check("unit-tests"), own, same_name_elsewhere], own_suite_ids=[500]))
        self.assertEqual((d["decision"], d["reason"], d["evidence"]["ci_failing"]), ("NEEDS_FIX", "CI_FAILING", 1))
        d = run_gate(clean_facts(check_runs=[check("unit-tests"), own], own_suite_ids=[]))
        self.assertEqual(d["reason"], "CI_PENDING")  # unidentified: counted, so it cannot pass early


class StaleReviewTests(unittest.TestCase):
    def test_findings_on_an_old_head_are_ignored(self):
        f = facts(sha(2), reviews=[review(101, sha(1))], review_comments=[inline(101)], check_runs=[check("unit-tests")])
        d = run_gate(f)
        self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("WAIT", "AWAITING_CODEX_REVIEW", None))
        self.assertEqual((d["evidence"]["codex_stale"], d["evidence"]["codex_findings"]), (1, 0))

    def test_a_clean_summary_for_an_old_head_cannot_make_a_new_head_ready(self):
        f = facts(sha(2), issue_comments=[summary(sha(1))], check_runs=[check("unit-tests")])
        self.assertEqual(run_gate(f)["decision"], "WAIT")
        f = facts(sha(2), issue_comments=[summary(sha(2), other_commit=sha(1))], check_runs=[check("unit-tests")])
        self.assertEqual(run_gate(f)["decision"], "READY")  # the row for the CURRENT head decides

    def test_stale_findings_do_not_block_a_clean_new_head(self):
        h = sha(2)
        f = facts(h, reviews=[review(101, sha(1))], review_comments=[inline(101)], issue_comments=[summary(h)], check_runs=[check("unit-tests")])
        self.assertEqual(run_gate(f)["decision"], "READY")

    def test_a_late_event_for_an_old_review_is_just_a_reevaluation(self):
        f = facts(sha(2), reviews=[review(101, sha(1))], review_comments=[inline(101)])
        f["event"] = {"name": "pull_request_review", "key": "review:101"}
        d = run_gate(f)
        self.assertEqual((d["decision"], d["handoff"], d["state"]["attempts"]), ("WAIT", None, []))

    def test_the_sessions_own_commit_finishes_the_previous_attempt(self):
        f = facts(sha(2), state=state(attempt(1, sha(1), "in_flight", at=g.NOW - 100, lease=g.NOW + 5000)),
                  head_commit=session_commit(1, sha(1)))
        d = run_gate(f)
        self.assertEqual((d["state"]["attempts"][0]["status"], d["state"]["attempts"][0]["next_head"], d["clear_label"]),
                         ("superseded", sha(2), True))
        self.assertEqual(d["reason"], "AWAITING_CODEX_REVIEW")


class IdentityTests(unittest.TestCase):
    def test_lookalike_codex_cannot_trigger_a_fix(self):
        h = sha(1)
        for who in (LOOKALIKE, {**CODEX, "type": "User"}, {**CODEX, "login": "chatgpt-codex-connector"}):
            with self.subTest(who=who):
                f = facts(h, reviews=[review(101, h, who)], review_comments=[inline(101)])
                self.assertEqual(run_gate(f)["decision"], "WAIT")

    def test_lookalike_summary_cannot_make_a_pr_ready(self):
        h = sha(1)
        self.assertEqual(run_gate(facts(h, issue_comments=[summary(h, who=LOOKALIKE)], check_runs=[check("unit-tests")]))["decision"], "WAIT")

    def test_a_lookalike_that_requests_changes_blocks_like_any_human(self):
        h = sha(1)
        d = run_gate(clean_facts(h, reviews=[review(7, h, LOOKALIKE, "CHANGES_REQUESTED")]))
        self.assertEqual(d["reason"], "HUMAN_CHANGES_REQUESTED")


class RepeatedEventTests(unittest.TestCase):
    def test_the_same_event_twice_hands_off_once(self):
        sim = g.Simulator()
        sim.facts = findings_facts()
        first = sim.event("pull_request_review", "review:101")
        second = sim.event("pull_request_review", "review:101")
        self.assertEqual((first["decision"], second["decision"], second["reason"], second["duplicate"]),
                         ("NEEDS_FIX", "WAIT", "HANDOFF_IN_FLIGHT", True))
        self.assertEqual(len(sim.handoffs), 1)
        self.assertEqual(len(sim.state["attempts"]), 1)

    def test_a_burst_of_distinct_events_on_one_head_hands_off_once(self):
        sim = g.Simulator()
        sim.facts = findings_facts()
        for i in range(12):
            sim.event("pull_request_review", "review:%d" % (200 + i))
        self.assertEqual(len(sim.handoffs), 1)

    def test_reevaluating_unchanged_facts_changes_no_state(self):
        sim = g.Simulator()
        sim.facts = findings_facts()
        sim.event("pull_request_review", "review:101")
        again = sim.event("pull_request_review", "review:101")
        again = sim.event("pull_request_review", "review:101")
        self.assertFalse(again["state_changed"])

    def test_event_key_memory_is_bounded_and_null_keys_are_never_duplicates(self):
        sim = g.Simulator()
        for i in range(40):
            sim.event("e", "k:%d" % i)
        self.assertEqual(len(sim.state["seen"]), 20)
        self.assertFalse(sim.event("workflow_dispatch", None)["duplicate"])

    def test_a_notification_is_sent_once_per_change_not_per_event(self):
        sim = g.Simulator()
        sim.facts = clean_facts()
        flags = [sim.event("pull_request_review", "review:%d" % i)["notify"] for i in range(3)]
        self.assertEqual(flags, [True, False, False])


class OverlapTests(unittest.TestCase):
    def test_no_second_handoff_while_one_is_in_flight(self):
        f = findings_facts(state=state(attempt(1, sha(1), "in_flight", at=g.NOW - 600, lease=g.NOW + 4800)))
        d = run_gate(f)
        self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("WAIT", "HANDOFF_IN_FLIGHT", None))

    def test_an_expired_lease_blocks_instead_of_starting_an_overlapping_session(self):
        f = findings_facts(state=state(attempt(1, sha(1), "in_flight", at=g.NOW - 7000, lease=g.NOW - 100)))
        d = run_gate(f)
        self.assertEqual((d["decision"], d["reason"], d["handoff"], d["state"]["attempts"][0]["status"]),
                         ("BLOCKED", "HANDOFF_TIMEOUT", None, "timed_out"))

    def test_a_timed_out_head_stays_blocked_and_does_not_renotify(self):
        sim = g.Simulator()
        sim.facts = findings_facts()
        sim.event("review", "review:101")
        sim.facts["now"] = g.NOW + 91 * 60
        first = sim.event("workflow_dispatch", None)
        second = sim.event("workflow_dispatch", None)
        self.assertEqual((first["reason"], first["notify"], second["reason"], second["notify"], len(sim.handoffs)),
                         ("HANDOFF_TIMEOUT", True, "HANDOFF_TIMEOUT", False, 1))

    def test_state_is_per_head(self):
        # an in-flight attempt for the OLD head never blocks work on a NEW head...
        f = findings_facts(sha(2), reviews=[review(102, sha(2))], review_comments=[inline(102)],
                           state=state(attempt(1, sha(1), "in_flight", at=g.NOW - 600, lease=g.NOW + 4800)),
                           head_commit=session_commit(1, sha(1)))
        d = run_gate(f)
        self.assertEqual((d["decision"], d["handoff"]["attempt"]), ("NEEDS_FIX", 2))
        self.assertEqual(d["state"]["attempts"][0]["status"], "superseded")
        # ...and exactly one attempt is in flight afterwards
        self.assertEqual([a["status"] for a in d["state"]["attempts"]].count("in_flight"), 1)


class OverlapRegressionTests(unittest.TestCase):
    """Security review finding: a push that was not the correction session's used to END the in-flight attempt,
    so a second session could start while the first was still running."""

    def inflight_state(self):
        return state(attempt(1, sha(1), "in_flight", at=g.NOW - 600, lease=g.NOW + 4800))

    def new_head_with_findings(self, commit):
        return findings_facts(sha(2), reviews=[review(102, sha(2))], review_comments=[inline(102)],
                              state=self.inflight_state(), head_commit=commit)

    def test_a_foreign_push_does_not_end_the_attempt_or_start_a_second_session(self):
        for label, commit in (("person", {"subject": "fix typo", "parents": [sha(1)]}),
                              ("rebase", {"subject": "Address review findings (bridge attempt 1/3)", "parents": [sha(9)]}),
                              ("merge commit", {"subject": "Address review findings (bridge attempt 1/3)", "parents": [sha(1), sha(7)]}),
                              ("title of a different attempt", {"subject": "Address review findings (bridge attempt 2/3)", "parents": [sha(1)]}),
                              ("title with a suffix", {"subject": "Address review findings (bridge attempt 1/3) please", "parents": [sha(1)]})):
            with self.subTest(label):
                d = run_gate(self.new_head_with_findings(commit))
                self.assertEqual((d["decision"], d["reason"], d["handoff"], d["clear_label"]), ("WAIT", "HANDOFF_IN_FLIGHT", None, False))
                self.assertEqual([a["status"] for a in d["state"]["attempts"]], ["in_flight"])

    def test_only_the_sessions_exact_commit_on_the_attempt_head_ends_it(self):
        d = run_gate(self.new_head_with_findings(session_commit(1, sha(1))))
        self.assertEqual((d["decision"], d["handoff"]["attempt"], d["clear_label"]), ("NEEDS_FIX", 2, True))
        self.assertEqual([a["status"] for a in d["state"]["attempts"]], ["superseded", "in_flight"])

    def test_after_a_foreign_push_the_lease_expiry_blocks_and_releases_the_label(self):
        f = self.new_head_with_findings({"subject": "fix typo", "parents": [sha(1)]}); f["now"] = g.NOW + 4801
        d = run_gate(f)
        self.assertEqual((d["decision"], d["reason"], d["handoff"], d["clear_label"], d["notify"]), ("BLOCKED", "HANDOFF_TIMEOUT", None, True, True))
        self.assertEqual(d["state"]["attempts"][0]["status"], "timed_out")

    def test_a_person_pushing_after_a_timeout_starts_a_fresh_evaluation(self):
        timed_out = state(attempt(1, sha(1), "timed_out"))
        f = findings_facts(sha(2), reviews=[review(102, sha(2))], review_comments=[inline(102)], state=timed_out,
                           head_commit={"subject": "fix typo", "parents": [sha(1)]})
        self.assertEqual(run_gate(f)["handoff"]["attempt"], 2)

    def test_simulated_loop_with_a_foreign_push_never_overlaps(self):
        s = g.Simulator()
        s.facts = facts(reviews=[review(101, sha(1))], review_comments=[inline(101)], check_runs=[check("unit-tests")])
        s.event("review", "review:101")
        s.push(2, foreign=True)
        s.facts["reviews"].append(review(102, sha(2))); s.facts["review_comments"].append(inline(102))
        d = s.event("review", "review:102")
        self.assertEqual((d["reason"], len(s.handoffs)), ("HANDOFF_IN_FLIGHT", 1))

    def test_the_commit_title_comes_from_config_and_matches_the_routine_prompt(self):
        self.assertEqual(g.CONFIG["fix_commit_title"], "Address review findings (bridge attempt {n}/{max})")
        prompt = (g.GATE / "routine-prompt.md").read_text()
        self.assertIn('titled "Address review findings (bridge attempt N/3)"', prompt)


class AttemptLimitTests(unittest.TestCase):
    def test_third_attempt_is_allowed_fourth_is_not(self):
        two = state(attempt(1, sha(1)), attempt(2, sha(2)))
        d = run_gate(findings_facts(sha(3), reviews=[review(103, sha(3))], review_comments=[inline(103)], state=two))
        self.assertEqual((d["decision"], d["handoff"]["attempt"]), ("NEEDS_FIX", 3))
        three = state(attempt(1, sha(1)), attempt(2, sha(2)), attempt(3, sha(3)))
        d = run_gate(findings_facts(sha(4), reviews=[review(104, sha(4))], review_comments=[inline(104)], state=three))
        self.assertEqual((d["decision"], d["reason"], d["handoff"], d["notify"]), ("MAX_ATTEMPTS", "ATTEMPT_LIMIT", None, True))

    def test_failed_and_timed_out_attempts_count_toward_the_limit(self):
        used = state(attempt(1, sha(1), "timed_out"), attempt(2, sha(2), "superseded"), attempt(3, sha(3), "superseded"))
        d = run_gate(findings_facts(sha(4), reviews=[review(104, sha(4))], review_comments=[inline(104)], state=used))
        self.assertEqual(d["decision"], "MAX_ATTEMPTS")

    def test_the_limit_is_persistent_state_not_commit_counts(self):
        # Many commits, no recorded attempts: the limit is untouched. Recorded attempts: it is used up.
        many_heads = findings_facts(sha(50), reviews=[review(150, sha(50))], review_comments=[inline(150)])
        self.assertEqual(run_gate(many_heads)["handoff"]["attempt"], 1)

    def test_limit_is_configurable_and_zero_means_never(self):
        cfg = copy.deepcopy(g.CONFIG); cfg["max_attempts"] = 0
        self.assertEqual(run_gate(findings_facts(), cfg)["decision"], "MAX_ATTEMPTS")

    def test_after_the_limit_a_clean_head_can_still_be_ready(self):
        used = state(attempt(1, sha(1)), attempt(2, sha(2)), attempt(3, sha(3)))
        d = run_gate(clean_facts(sha(4), state=used))
        self.assertEqual(d["decision"], "READY")

    def test_full_loop_is_capped_at_three_handoffs(self):
        s = g.Simulator()
        s.event("review", "review:1", lambda f: f.update(reviews=[review(1, sha(1))], review_comments=[inline(1)], check_runs=[check("unit-tests")]))
        for n in (2, 3, 4):
            s.push(n)
            s.event("synchronize", "head:%d" % n)
            s.event("review", "review:%d" % n, lambda f, n=n: (f["reviews"].append(review(n, sha(n))), f["review_comments"].append(inline(n))))
        self.assertEqual(len(s.handoffs), 3)
        self.assertEqual(s.state["last"]["decision"], "MAX_ATTEMPTS")


class FailClosedTests(unittest.TestCase):
    def assert_blocked_untouched(self, f):
        d = run_gate(f)
        self.assertEqual((d["decision"], d["reason"]), ("BLOCKED", "EVIDENCE_UNAVAILABLE"), d)
        self.assertEqual((d["handoff"], d["notify"], d["state_changed"], d["state"]), (None, False, False, f.get("state")))
        return d

    def test_any_recorded_api_failure_blocks_even_with_findings(self):
        for marker in ("reviews", "review_comments", "all_comments", "check_runs", "status", "runs", "state_corrupt", "input", "head", "head_commit"):
            with self.subTest(marker):
                d = self.assert_blocked_untouched(findings_facts(incomplete=[marker]))
                self.assertIn(marker, d["problems"])

    def test_malformed_or_missing_evidence_blocks(self):
        cases = {
            "pr missing": {"pr": None},
            "short head": {"pr": {"number": 7, "state": "open", "merged": False, "draft": False, "head_sha": "abc", "head_repo": "o/r",
                                  "base_repo": "o/r", "labels": []}},
            "reviews missing": {"reviews": None},
            "reviews not a list": {"reviews": {"a": 1}},
            "comments missing": {"issue_comments": None},
            "check runs missing": {"check_runs": None},
            "own suites missing": {"own_suite_ids": None},
            "status missing": {"combined_status": None},
            "status for another commit": {"combined_status": {"state": "pending", "total": 0, "listed": 0, "sha": sha(9)}},
            "status truncated": {"combined_status": {"state": "success", "total": 5, "listed": 2, "sha": sha(1)}},
            "status state unknown": {"combined_status": {"state": "weird", "total": 0, "listed": 0, "sha": sha(1)}},
            "unknown conclusion": {"check_runs": [check("t", conclusion="mystery")]},
            "unknown status": {"check_runs": [{"name": "t", "status": None, "conclusion": None, "suite_id": 1}]},
            "state wrong version": {"state": {"v": 2, "attempts": [], "seen": [], "last": None}},
            "state not an object": {"state": "oops"},
            "state attempts not a list": {"state": {"v": 1, "attempts": None, "seen": [], "last": None}},
            "no clock": {"now": None},
            "head commit missing": {"head_commit": None},
            "head commit without parents list": {"head_commit": {"subject": "x"}},
            "head commit subject not text": {"head_commit": {"subject": 5, "parents": []}},
            "incomplete malformed": {"incomplete": "reviews"},
        }
        for label, over in cases.items():
            with self.subTest(label):
                self.assert_blocked_untouched(findings_facts(**over))

    def test_evidence_problems_beat_every_other_decision(self):
        f = findings_facts(kill_switch=True, incomplete=["reviews"])
        self.assertEqual(run_gate(f)["reason"], "EVIDENCE_UNAVAILABLE")

    def test_an_api_failure_after_a_handoff_leaves_the_recorded_attempt_alone(self):
        sim = g.Simulator()
        sim.facts = findings_facts()
        sim.event("review", "review:101")
        before = copy.deepcopy(sim.state)
        sim.event("review", "review:102", incomplete=["check_runs"])
        self.assertEqual(sim.state, before)
        self.assertEqual(len(sim.handoffs), 1)

    def test_recovery_after_a_transient_failure_resumes_without_double_handoff(self):
        sim = g.Simulator()
        sim.facts = findings_facts()
        sim.event("review", "review:101", incomplete=["reviews"])
        self.assertEqual(sim.handoffs, [])
        sim.event("review", "review:101", incomplete=[])
        sim.event("review", "review:101")
        self.assertEqual(len(sim.handoffs), 1)


if __name__ == "__main__":
    unittest.main()
