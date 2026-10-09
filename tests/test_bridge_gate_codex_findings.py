"""Regression tests for two Codex review findings on PR #1 (both verified as real defects).

1. "Check the activation lock before recording an attempt": with real mode on but the default-branch lock closed or
   unreadable, the gate used to record an `in_flight` attempt, then the real step exited without a label. That left a
   false in-flight state that blocked the PR for the whole lease and then counted toward the three-attempt limit.
2. "Require the designated CI check before declaring READY": any successful check run or legacy commit status used to
   count as CI, so an unrelated check (or `unit-tests` from another app) could stand in for the required one.

No network and no AI: the real gate.jq / collect.sh / workflow steps run against a fake GitHub.
"""

import json
import re
import unittest

from . import gate_sim as g
from .gate_sim import CODEX, GITHUB_ACTIONS_APP_ID, check, facts, inline, review, run_gate, sha, summary
from .test_bridge_gate_collect import Fixture
from .test_bridge_gate_workflow import (COLLECT, DECIDE, GUARD, NOTIFY, PERSIST, REAL, Runner, findings_fixture,
                                        script_of, step_blocks)

H = sha(1)
CFG = g.CONFIG
PRE_REAL = [COLLECT, DECIDE, GUARD, PERSIST, NOTIFY]


def clean(checks, **over):
    """A completed, clean Codex review of the current head, with the given check runs."""
    return facts(H, issue_comments=[summary(H)], check_runs=checks, **over)


def with_finding(**over):
    return facts(H, reviews=[review(101, H)], review_comments=[inline(101)], check_runs=[check("unit-tests")], **over)


def attempt(n, head, status="superseded"):
    return {"n": n, "head": head, "trigger": "x", "reason": "CODEX_FINDINGS", "at": g.NOW - 10_000,
            "lease_expires": g.NOW - 5_000, "status": status}


class DesignatedCiCheckTests(unittest.TestCase):
    """Finding 2."""

    def test_the_designated_check_from_the_designated_app_declares_ready(self):
        d = run_gate(clean([check("unit-tests")]))
        self.assertEqual((d["decision"], d["reason"]), ("READY", "CODEX_CLEAN_AND_CI_PASSING"))
        self.assertEqual(d["evidence"]["ci_passing"], 1)

    def test_an_unrelated_passing_check_cannot_stand_in(self):
        d = run_gate(clean([check("coverage-report"), check("lint", suite=101)]))
        self.assertEqual((d["decision"], d["reason"]), ("WAIT", "CI_MISSING"))
        self.assertEqual(d["evidence"]["ci_passing"], 0)

    def test_a_legacy_commit_status_cannot_stand_in(self):
        d = run_gate(clean([], combined_status={"state": "success", "total": 2, "listed": 2, "sha": H}))
        self.assertEqual((d["decision"], d["reason"]), ("WAIT", "CI_MISSING"))

    def test_the_right_name_from_the_wrong_source_cannot_stand_in(self):
        for app in (99999, None, 0, "15368", "github-actions"):
            run = {**check("unit-tests"), "app_id": app}
            d = run_gate(clean([run]))
            self.assertEqual((d["decision"], d["reason"]), ("WAIT", "CI_MISSING"), app)
        no_app = check("unit-tests"); del no_app["app_id"]
        self.assertEqual(run_gate(clean([no_app]))["reason"], "CI_MISSING")

    def test_name_must_match_exactly(self):
        for name in ("Unit-Tests", "unit-tests ", "unit-test", "ci / unit-tests", "unit-tests-extra"):
            self.assertEqual(run_gate(clean([check(name)]))["reason"], "CI_MISSING", name)

    def test_a_designated_check_that_did_not_succeed_is_not_a_pass(self):
        for conclusion in ("skipped", "neutral"):
            d = run_gate(clean([check("unit-tests", conclusion=conclusion)]))
            self.assertEqual((d["decision"], d["reason"]), ("WAIT", "CI_MISSING"), conclusion)

    def test_the_gates_own_suite_cannot_satisfy_it(self):
        d = run_gate(clean([check("unit-tests", suite=500)], own_suite_ids=[500]))
        self.assertEqual((d["decision"], d["reason"]), ("WAIT", "CI_MISSING"))

    def test_an_unrelated_success_while_the_designated_check_has_not_finished_keeps_waiting(self):
        d = run_gate(clean([check("coverage-report"), check("unit-tests", "in_progress", None, suite=101)]))
        self.assertEqual((d["decision"], d["reason"]), ("WAIT", "CI_PENDING"))

    def test_existing_ci_failure_handling_is_preserved(self):
        d = run_gate(clean([check("unit-tests", conclusion="failure")]))
        self.assertEqual((d["decision"], d["reason"], d["handoff"]["attempt"]), ("NEEDS_FIX", "CI_FAILING", 1))
        # any failing independent check still forces a fix, even with the designated check green
        d = run_gate(clean([check("unit-tests"), check("coverage-report", conclusion="failure", suite=101)]))
        self.assertEqual((d["decision"], d["reason"]), ("NEEDS_FIX", "CI_FAILING"))
        # a failing legacy status too
        d = run_gate(clean([check("unit-tests")], combined_status={"state": "failure", "total": 1, "listed": 1, "sha": H}))
        self.assertEqual(d["reason"], "CI_FAILING")

    def test_the_three_attempt_limit_still_applies_to_ci_failures(self):
        state = {"v": 1, "attempts": [attempt(1, sha(11)), attempt(2, sha(12)), attempt(3, sha(13))], "seen": [], "last": None}
        d = run_gate(clean([check("unit-tests", conclusion="failure")], state=state))
        self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("MAX_ATTEMPTS", "ATTEMPT_LIMIT", None))

    def test_require_ci_false_is_unchanged_and_require_ci_stays_true(self):
        self.assertIs(CFG["require_ci"], True)
        d = run_gate(clean([]), config={**CFG, "require_ci": False})
        self.assertEqual(d["decision"], "READY")
        d = run_gate(clean([check("coverage-report")]), config={**CFG, "require_ci": True})
        self.assertEqual(d["reason"], "CI_MISSING")

    def test_the_designation_is_configuration_and_matches_ci_and_the_ruleset(self):
        self.assertEqual(CFG["ci_check"], {"name": "unit-tests", "app_id": GITHUB_ACTIONS_APP_ID})
        self.assertEqual(GITHUB_ACTIONS_APP_ID, 15368)   # the ruleset's required check integration_id
        ci = (g.ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertRegex(ci, r"(?m)^  unit-tests:$")        # the job name is the check name
        self.assertNotIn("unit-tests", (g.GATE / "gate.jq").read_text())   # gate.jq stays config-driven
        # a designation that is absent or malformed can only make READY unreachable, never easier
        for cfg in ({k: v for k, v in CFG.items() if k != "ci_check"}, {**CFG, "ci_check": {}}, {**CFG, "ci_check": None}):
            self.assertEqual(run_gate(clean([check("unit-tests")]), config=cfg)["reason"], "CI_MISSING")

    def test_the_collector_reports_which_app_produced_each_check(self):
        fx = Fixture(H)
        fx.put("commits_SHA_check-runs", {"total_count": 2, "check_runs": [
            {"name": "unit-tests", "status": "completed", "conclusion": "success", "check_suite": {"id": 1}, "app": {"id": 15368, "slug": "github-actions"}},
            {"name": "unit-tests", "status": "completed", "conclusion": "success", "check_suite": {"id": 2}, "app": {"id": 424242}},
            {"name": "unit-tests", "status": "completed", "conclusion": "success", "check_suite": {"id": 3}}]})
        runs = fx.collect()["check_runs"]
        self.assertEqual([r["app_id"] for r in runs], [15368, 424242, None])

    def test_end_to_end_ready_needs_the_designated_check_from_the_designated_app(self):
        def ready_for(check_run):
            fx = Fixture(H)
            fx.put("issues_7_comments", [{"id": 9000, "user": {"login": CODEX["login"], "id": CODEX["uid"], "type": CODEX["type"]},
                                         "body": summary(H)["body"]}])
            fx.put("commits_SHA_check-runs", {"total_count": 1, "check_runs": [check_run]})
            r = Runner(fx)
            r.run(steps=[COLLECT, DECIDE])
            return r.decision()["decision"]
        base = {"status": "completed", "conclusion": "success", "check_suite": {"id": 100}}
        self.assertEqual(ready_for({**base, "name": "unit-tests", "app": {"id": 15368}}), "READY")
        self.assertEqual(ready_for({**base, "name": "unit-tests", "app": {"id": 777}}), "WAIT")
        self.assertEqual(ready_for({**base, "name": "unit-tests"}), "WAIT")
        self.assertEqual(ready_for({**base, "name": "coverage", "app": {"id": 15368}}), "WAIT")


class LockBeforeAttemptTests(unittest.TestCase):
    """Finding 1: a handoff that cannot start must never be recorded as started."""

    CLOSED_STATES = (False, None, "true", 1, "yes", [], {})

    def test_label_mode_with_a_lock_that_is_not_confirmed_open_records_no_attempt(self):
        for lock in self.CLOSED_STATES:
            d = run_gate(with_finding(handoff_mode="label", handoff_lock_open=lock))
            self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("BLOCKED", "HANDOFF_LOCKED", None), lock)
            self.assertEqual(d["state"]["attempts"], [], lock)
            self.assertFalse(d["clear_label"])
            self.assertTrue(d["notify"])                      # a person is told once

    def test_a_missing_lock_fact_in_label_mode_fails_closed(self):
        f = with_finding(handoff_mode="label")
        self.assertNotIn("handoff_lock_open", f)
        d = run_gate(f)
        self.assertEqual((d["decision"], d["reason"], d["state"]["attempts"]), ("BLOCKED", "HANDOFF_LOCKED", []))

    def test_an_open_lock_still_hands_off_exactly_as_before(self):
        d = run_gate(with_finding(handoff_mode="label", handoff_lock_open=True))
        self.assertEqual((d["decision"], d["reason"]), ("NEEDS_FIX", "CODEX_FINDINGS"))
        self.assertEqual((d["handoff"]["attempt"], d["handoff"]["of"], d["handoff"]["mode"]), (1, 3, "label"))
        self.assertEqual([(a["n"], a["status"]) for a in d["state"]["attempts"]], [(1, "in_flight")])

    def test_simulate_mode_is_unchanged_a_simulated_attempt_is_still_recorded(self):
        for lock in (None, False):
            d = run_gate(with_finding(handoff_mode="simulate", handoff_lock_open=lock))
            self.assertEqual((d["decision"], d["handoff"]["mode"]), ("NEEDS_FIX", "simulate"), lock)

    def test_ci_failures_are_covered_by_the_same_rule(self):
        d = run_gate(clean([check("unit-tests", conclusion="failure")], handoff_mode="label", handoff_lock_open=False))
        self.assertEqual((d["decision"], d["reason"], d["handoff"], d["state"]["attempts"]), ("BLOCKED", "HANDOFF_LOCKED", None, []))

    def test_the_attempt_limit_keeps_precedence_and_inflight_state_is_untouched(self):
        used = {"v": 1, "attempts": [attempt(1, sha(11)), attempt(2, sha(12)), attempt(3, sha(13))], "seen": [], "last": None}
        d = run_gate(with_finding(handoff_mode="label", handoff_lock_open=False, state=used))
        self.assertEqual((d["decision"], d["reason"]), ("MAX_ATTEMPTS", "ATTEMPT_LIMIT"))
        flight = {"v": 1, "attempts": [attempt(1, H, "in_flight")], "seen": [], "last": None}
        flight["attempts"][0]["lease_expires"] = g.NOW + 1000
        d = run_gate(with_finding(handoff_mode="label", handoff_lock_open=False, state=flight))
        self.assertEqual((d["decision"], d["reason"]), ("WAIT", "HANDOFF_IN_FLIGHT"))

    def test_decisions_that_hand_nothing_off_ignore_the_lock(self):
        d = run_gate(clean([check("unit-tests")], handoff_mode="label", handoff_lock_open=False))
        self.assertEqual(d["decision"], "READY")
        d = run_gate(facts(H, handoff_mode="label", handoff_lock_open=False))
        self.assertEqual(d["decision"], "WAIT")

    def test_a_closed_lock_never_burns_attempts_across_repeated_events_and_the_first_real_attempt_is_number_one(self):
        state = None
        for n in range(5):                                       # five events while the lock is closed
            d = run_gate(with_finding(handoff_mode="label", handoff_lock_open=False, state=state, event={"name": "x", "key": "k%d" % n}))
            state = d["state"]
            self.assertEqual((d["decision"], state["attempts"]), ("BLOCKED", []), n)
        d = run_gate(with_finding(handoff_mode="label", handoff_lock_open=True, state=state, event={"name": "x", "key": "k9"}))
        self.assertEqual((d["decision"], d["handoff"]["attempt"]), ("NEEDS_FIX", 1))   # not attempt 6, not attempt 2

    def test_the_lock_fact_is_read_only_in_real_mode_from_the_default_branch(self):
        fx = Fixture(H)
        fx.put("contents_bridge-gate_config.json", {**CFG, "real_handoff_enabled": True})
        sim = fx.collect(DEFAULT_BRANCH="main")
        self.assertIsNone(sim["handoff_lock_open"])
        self.assertFalse([c for c in fx.calls() if "contents/" in c])               # simulate mode makes no lock call
        real = fx.collect(HANDOFF_MODE="label", DEFAULT_BRANCH="main")
        self.assertIs(real["handoff_lock_open"], True)
        self.assertTrue([c for c in fx.calls() if "contents/bridge-gate/config.json?ref=main" in c])

    def test_every_unreadable_or_not_true_lock_reads_as_closed_in_the_collector(self):
        for label, setup in (("false", lambda f: f.put("contents_bridge-gate_config.json", {**CFG, "real_handoff_enabled": False})),
                             ("string true", lambda f: f.put("contents_bridge-gate_config.json", {**CFG, "real_handoff_enabled": "true"})),
                             ("garbage", lambda f: (f.dir / "contents_bridge-gate_config.json.json").write_text("<html>")),
                             ("missing file", lambda f: None),
                             ("api error", lambda f: (f.put("contents_bridge-gate_config.json", {**CFG, "real_handoff_enabled": True}), f.fail("contents_bridge-gate_config.json")))):
            fx = Fixture(H)
            setup(fx)
            self.assertIs(fx.collect(HANDOFF_MODE="label", DEFAULT_BRANCH="main")["handoff_lock_open"], False, label)

    def test_a_hostile_default_branch_name_is_never_used(self):
        fx = Fixture(H)
        fx.put("contents_bridge-gate_config.json", {**CFG, "real_handoff_enabled": True})
        for bad in ("main;touch pwned", "$(touch pwned)", "", "a b", "../x"):
            got = fx.collect(HANDOFF_MODE="label", DEFAULT_BRANCH=bad)["handoff_lock_open"]
            self.assertIs(got, False, bad)
        self.assertFalse([c for c in fx.calls() if "touch" in c])

    def test_pipeline_closed_lock_writes_a_block_notice_and_no_attempt_and_no_label(self):
        for how in ("closed", "unreadable"):
            fx = findings_fixture()
            if how == "unreadable":
                fx.fail("contents_bridge-gate_config.json")
            r = Runner(fx, HANDOFF_MODE="label")                # Runner default: lock closed on the default branch
            out = r.run(steps=PRE_REAL + [REAL], HANDOFF_MODE="label")
            self.assertEqual([p.returncode for p in out.values()], [0] * 6, {k: v.stderr for k, v in out.items()})
            d = r.decision()
            self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("BLOCKED", "HANDOFF_LOCKED", None), how)
            saved = json.loads(r.bodies()[0].read_text().splitlines()[0][len("<!-- bridge-gate-state:v1 "):-len(" -->")])
            self.assertEqual(saved["attempts"], [], how)
            self.assertEqual([w for w in fx.writes() if "labels" in w], [], how)
            self.assertIn("HANDOFF_LOCKED", r.bodies()[1].read_text())
            # the same event delivered again still creates nothing
            fx2 = findings_fixture()
            fx2.put("issues_7_comments", [{"id": 4242, "user": {"login": "github-actions[bot]", "id": 41898282, "type": "Bot"}, "body": r.bodies()[0].read_text()}])
            if how == "unreadable":
                fx2.fail("contents_bridge-gate_config.json")
            r2 = Runner(fx2, HANDOFF_MODE="label")
            r2.run(steps=PRE_REAL + [REAL], HANDOFF_MODE="label")
            self.assertEqual(r2.decision()["state"]["attempts"], [], how)

    def test_pipeline_open_lock_persists_the_attempt_first_then_adds_exactly_one_label(self):
        fx = findings_fixture()
        r = Runner(fx, unlock=True, HANDOFF_MODE="label")
        out = r.run(steps=PRE_REAL + [REAL], HANDOFF_MODE="label")
        self.assertEqual([p.returncode for p in out.values()], [0] * 6, {k: v.stderr for k, v in out.items()})
        self.assertEqual(r.decision()["decision"], "NEEDS_FIX")
        kinds = [w.split()[0] + (" label" if "/labels" in w else " comment") for w in fx.writes()]
        self.assertEqual(kinds, ["POST comment", "POST label"])              # write-ahead order preserved
        self.assertEqual(len([w for w in fx.writes() if "/labels" in w]), 1)

    def test_simulate_pipeline_is_unchanged(self):
        fx = findings_fixture()
        r = Runner(fx)
        r.run()
        self.assertEqual((r.decision()["decision"], r.decision()["handoff"]["mode"]), ("NEEDS_FIX", "simulate"))

    def test_the_real_step_still_rechecks_the_lock_as_defence_in_depth_and_only_the_collect_step_gained_an_input(self):
        self.assertIn("real_handoff_enabled", script_of(REAL).splitlines()[0])
        self.assertIn("DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}", step_blocks()[COLLECT])
        self.assertIs(CFG["real_handoff_enabled"], False)
        self.assertEqual(len(re.findall(r"vars\.BRIDGE_HANDOFF_MODE", (g.ROOT / ".github/workflows/bridge-gate.yml").read_text())), 3)


if __name__ == "__main__":
    unittest.main()
