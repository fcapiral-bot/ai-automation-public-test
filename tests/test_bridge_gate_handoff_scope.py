"""The real handoff can be scoped to one family of disposable branches, so a live test does not expose the whole repository.

`handoff_branch_prefix` in the default branch's bridge-gate/config.json narrows where a label may ever be applied: a pull request whose head branch
does not start with it gets `WAIT / HANDOFF_OUT_OF_SCOPE`, no attempt and no label. It only narrows: the attempt limit, the lease, the lock and the
human-only merge rule are untouched, and simulate mode behaves exactly as before. No network, no GitHub, no AI, no Routine.
"""

import json
import unittest

from . import gate_sim as g
from .gate_sim import run_gate
from .test_bridge_gate_codex_findings import check, clean, with_finding
from .test_bridge_gate_collect import Fixture
from .test_bridge_gate_retry_chain import ADD, Chain
from .gate_sim import sha
from .test_bridge_gate_collect import BOT, state_comment_body
from .test_bridge_gate_effects_reliability import fail_write
from .test_bridge_gate_workflow import COLLECT, DECIDE, GUARD, PERSIST, REAL, Runner, findings_fixture

PREFIX = g.CONFIG["handoff_branch_prefix"]
OPEN = dict(handoff_mode="label", handoff_lock_open=True, handoff_scope=PREFIX)   # the facts the collector reads from the DEFAULT branch


def pr_on(ref, **over):
    """The finding fixture, on a head branch `ref` (None = the fact is missing)."""
    f = with_finding(**{**OPEN, **over})
    f["pr"] = {**f["pr"], "head_ref": ref}
    if ref is None:
        del f["pr"]["head_ref"]
    return f


class ScopeDecisionTests(unittest.TestCase):
    def test_the_shipped_scope_is_one_disposable_branch_family_and_nothing_is_activated(self):
        self.assertEqual(PREFIX, "claude/e2e-synthetic-4b-")
        self.assertIs(g.CONFIG["real_handoff_enabled"], g.REAL_HANDOFF_ENABLED)
        self.assertEqual((g.CONFIG["max_attempts"], g.CONFIG["lease_minutes"]), (3, 90))

    def test_a_branch_inside_the_scope_is_handed_off_exactly_as_before(self):
        d = run_gate(pr_on(PREFIX + "a"))
        self.assertEqual((d["decision"], d["reason"], d["handoff"]["attempt"], d["handoff"]["mode"]), ("NEEDS_FIX", "CODEX_FINDINGS", 1, "label"))

    def test_any_other_branch_waits_records_no_attempt_and_posts_nothing(self):
        for ref in ("claude/e2e-synthetic-c", "claude/e2e-synthetic-4", "claude/other", "main", "x" + PREFIX, PREFIX[:-1], "", None, 7, ["claude/e2e-synthetic-4b-a"]):
            d = run_gate(pr_on(ref))
            self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("WAIT", "HANDOFF_OUT_OF_SCOPE", None), repr(ref))
            self.assertEqual(d["state"]["attempts"], [], repr(ref))
            self.assertFalse(d["notify"]); self.assertFalse(d["clear_label"])

    def test_a_failing_ci_check_is_scoped_too(self):
        f = clean([check("unit-tests", conclusion="failure")], **OPEN)
        f["pr"] = {**f["pr"], "head_ref": "claude/other"}
        d = run_gate(f)
        self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("WAIT", "HANDOFF_OUT_OF_SCOPE", None))

    def test_a_garbled_prefix_fails_closed(self):
         for bad in (7, True, False, 0, ["claude/"], {"p": 1}):
            d = run_gate(pr_on(PREFIX + "a", handoff_scope=bad))
            self.assertEqual((d["decision"], d["handoff"]), ("WAIT", None), repr(bad))

    def test_without_a_prefix_nothing_is_scoped_and_simulate_mode_never_is(self):
        for none in (None, ""):
            d = run_gate(pr_on("claude/other", handoff_scope=none))
            self.assertEqual((d["decision"], d["handoff"]["attempt"]), ("NEEDS_FIX", 1))
        f = with_finding(handoff_mode="simulate")
        f["pr"] = {**f["pr"], "head_ref": "claude/other"}
        d = run_gate(f)
        self.assertEqual((d["decision"], d["handoff"]["mode"]), ("NEEDS_FIX", "simulate"))

    def test_the_lock_and_the_attempt_limit_keep_working_inside_the_scope(self):
        d = run_gate(pr_on(PREFIX + "a", handoff_lock_open=False))
        self.assertEqual((d["decision"], d["reason"]), ("BLOCKED", "HANDOFF_LOCKED"))
        used = [{"n": n, "head": g.sha(10 + n), "trigger": None, "reason": "CI_FAILING", "at": g.NOW - 9999, "lease_expires": g.NOW - 99, "status": "superseded"}
                for n in (1, 2, 3)]
        d = run_gate(pr_on(PREFIX + "a", state={"v": 1, "attempts": used, "seen": [], "last": None}))
        self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("MAX_ATTEMPTS", "ATTEMPT_LIMIT", None))


class ScopeCollectorTests(unittest.TestCase):
    """The scope comes from the DEFAULT branch's config, exactly like the lock: a pull request cannot widen it by editing its own copy."""

    def collect(self, default_cfg, mode="label"):
        fx = Fixture()
        if default_cfg is not None:
            fx.put("contents_bridge-gate_config.json", default_cfg)
        return fx.collect(KILL_SWITCH="", HANDOFF_MODE=mode, DEFAULT_BRANCH="main")

    def test_the_scope_is_read_from_the_default_branch_with_the_lock(self):
        facts = self.collect({**g.CONFIG, "real_handoff_enabled": True})
        self.assertEqual((facts["handoff_lock_open"], facts["handoff_scope"]), (True, PREFIX))
        self.assertEqual(self.collect({**g.CONFIG, "real_handoff_enabled": True, "handoff_branch_prefix": "claude/"})["handoff_scope"], "claude/")

    def test_a_pull_requests_own_config_is_never_consulted(self):
        # The checked-out tree here carries the shipped prefix; the default branch says otherwise, and the default branch wins.
        facts = self.collect({**g.CONFIG, "real_handoff_enabled": True, "handoff_branch_prefix": "claude/elsewhere-"})
        self.assertEqual(facts["handoff_scope"], "claude/elsewhere-")
        self.assertEqual(self.collect({k: v for k, v in {**g.CONFIG, "real_handoff_enabled": True}.items() if k != "handoff_branch_prefix"})["handoff_scope"], None)

    def test_a_closed_or_missing_lock_and_simulate_mode_carry_no_scope(self):
        self.assertEqual(self.collect({**g.CONFIG, "real_handoff_enabled": False})["handoff_scope"], None)
        self.assertEqual(self.collect(None)["handoff_scope"], None)
        self.assertEqual(self.collect({**g.CONFIG, "real_handoff_enabled": True}, mode="")["handoff_scope"], None)


class ScopeRecheckTests(unittest.TestCase):
    """label.sh re-reads the scope and the PR's CURRENT head branch right before the POST, like the lock (Codex findings on PR #15).

    The attempt is recorded before the label (write-ahead), so a refusal at that point must take back exactly that attempt: otherwise it waits
    out its 90-minute lease, notifies a person and uses up one of the three attempts although no Routine could ever have started."""

    def decided(self, prior=None):
        """Collect, decide, guard and PERSIST attempt N for an in-scope PR; the label step has not run yet. `prior`: earlier attempts."""
        fx = findings_fixture()
        if prior:
            fx.put("issues_7_comments", [{"id": 555, "user": BOT, "body": state_comment_body({"v": 1, "attempts": prior, "seen": [], "last": None})}])
        r = Runner(fx, unlock=True, HANDOFF_MODE="label")
        out = r.run(steps=[COLLECT, DECIDE, GUARD, PERSIST])
        self.assertEqual([p.returncode for p in out.values()], [0, 0, 0, 0], {k: v.stderr[-200:] for k, v in out.items()})
        d = r.decision()
        self.assertEqual((d["decision"], d["handoff"]["attempt"]), ("NEEDS_FIX", len(prior or []) + 1))
        if not prior:                                    # a fresh PR: the state comment was CREATED by this run, GitHub now lists it
            body = next(b.read_text() for b in r.bodies() if b.read_text().startswith("<!-- bridge-gate-state:v1 "))
            fx.put("issues_7_comments", [{"id": 555, "user": BOT, "body": body}])
        return fx, r

    def label_writes(self, fx):
        return [w for w in fx.writes() if "/labels" in w]

    def recorded(self, fx):
        """The attempts the state comment now holds (the last write to it)."""
        bodies = sorted(fx.dir.glob("body-*.txt"), key=lambda q: int(q.stem.split("-")[1]))
        last = [b.read_text() for b in bodies if b.read_text().startswith("<!-- bridge-gate-state:v1 ")][-1]
        return json.loads(last.splitlines()[0][len("<!-- bridge-gate-state:v1 "):-len(" -->")])

    def narrow(self, fx):
        fx.put("contents_bridge-gate_config.json", {**g.CONFIG, "real_handoff_enabled": True, "handoff_branch_prefix": "claude/e2e-synthetic-4b-z"})

    def rename(self, fx):
        pr = json.loads((fx.dir / "pulls_7.json").read_text())
        pr["head"]["ref"] = "claude/other"
        fx.put("pulls_7", pr)

    def test_in_scope_at_decision_time_and_still_in_scope_adds_the_label_and_keeps_the_attempt(self):
        fx, r = self.decided()
        self.assertEqual(r.run(steps=[REAL])[REAL].returncode, 0)
        self.assertEqual(len(self.label_writes(fx)), 1)
        self.assertEqual([a["status"] for a in self.recorded(fx)["attempts"]], ["in_flight"])

    def test_a_prefix_narrowed_after_the_attempt_was_recorded_withdraws_it_and_applies_no_label(self):
        fx, r = self.decided()
        self.narrow(fx)
        out = r.run(steps=[REAL])
        self.assertEqual(out[REAL].returncode, 0); self.assertIn("withdrawn", out[REAL].stdout)
        self.assertEqual(self.label_writes(fx), [])
        st = self.recorded(fx)
        self.assertEqual(st["attempts"], [])
        self.assertEqual((st["last"]["decision"], st["last"]["reason"]), ("WAIT", "HANDOFF_OUT_OF_SCOPE"))

    def test_a_branch_renamed_after_the_attempt_was_recorded_withdraws_it_and_applies_no_label(self):
        fx, r = self.decided()
        self.rename(fx)
        out = r.run(steps=[REAL])
        self.assertEqual(out[REAL].returncode, 0); self.assertIn("withdrawn", out[REAL].stdout)
        self.assertEqual((self.label_writes(fx), self.recorded(fx)["attempts"]), ([], []))

    def test_a_garbled_prefix_withdraws_too(self):
        for bad in (7, False, ["claude/"]):
            fx, r = self.decided()
            fx.put("contents_bridge-gate_config.json", {**g.CONFIG, "real_handoff_enabled": True, "handoff_branch_prefix": bad})
            self.assertEqual(r.run(steps=[REAL])[REAL].returncode, 0, bad)
            self.assertEqual((self.label_writes(fx), self.recorded(fx)["attempts"]), ([], []), bad)

    def test_only_the_attempt_this_run_recorded_is_withdrawn_and_the_count_stays_honest(self):
        prior = [{"n": 1, "head": sha(0xA), "trigger": None, "reason": "CI_FAILING", "at": g.NOW - 9000, "lease_expires": g.NOW - 3600, "status": "superseded", "next_head": sha(1)}]
        fx, r = self.decided(prior)
        self.assertEqual([(a["n"], a["status"]) for a in self.recorded(fx)["attempts"]], [(1, "superseded"), (2, "in_flight")])
        self.narrow(fx)
        self.assertEqual(r.run(steps=[REAL])[REAL].returncode, 0)
        self.assertEqual([(a["n"], a["status"]) for a in self.recorded(fx)["attempts"]], [(1, "superseded")])      # attempt 1 untouched, none consumed
        self.assertEqual(self.label_writes(fx), [])

    def test_after_a_withdrawal_the_next_evaluation_is_clean_and_the_cap_is_unchanged(self):
        fx, r = self.decided()
        self.narrow(fx)
        r.run(steps=[REAL])
        # Out of scope: later events wait, record nothing and apply nothing.
        fx.put("issues_7_comments", [{"id": 555, "user": BOT, "body": state_comment_body(self.recorded(fx))}])
        r2 = Runner(fx, HANDOFF_MODE="label"); self.narrow(fx)
        r2.run(steps=[COLLECT, DECIDE], EVENT_NAME="workflow_dispatch", EVENT_KEY="")
        d = r2.decision()
        self.assertEqual((d["decision"], d["reason"], d["handoff"], d["state"]["attempts"]), ("WAIT", "HANDOFF_OUT_OF_SCOPE", None, []))
        # Back in scope: a NEW attempt 1 (not 2), and the limit is still three.
        fx.put("contents_bridge-gate_config.json", {**g.CONFIG, "real_handoff_enabled": True})
        r3 = Runner(fx, unlock=True, HANDOFF_MODE="label")
        r3.run(steps=[COLLECT, DECIDE], EVENT_NAME="workflow_dispatch", EVENT_KEY="")
        d = r3.decision()
        self.assertEqual((d["decision"], d["handoff"]["attempt"], d["handoff"]["of"]), ("NEEDS_FIX", 1, 3))

    def test_if_the_withdrawal_cannot_be_written_the_step_fails_loudly_and_nothing_else_is_touched(self):
        fx, r = self.decided()
        self.narrow(fx)
        fail_write(fx, 2)                                  # write 1 was the persist; write 2 is the withdrawal
        out = r.run(steps=[REAL])
        self.assertNotEqual(out[REAL].returncode, 0); self.assertIn("could not be withdrawn", out[REAL].stderr)
        self.assertEqual(self.label_writes(fx), [])

    def test_an_unfindable_state_comment_fails_the_step_instead_of_guessing(self):
        fx, r = self.decided()
        self.narrow(fx)
        fx.put("issues_7_comments", [])
        out = r.run(steps=[REAL])
        self.assertNotEqual(out[REAL].returncode, 0); self.assertIn("state comment cannot be found", out[REAL].stderr)
        self.assertEqual(self.label_writes(fx), [])

    def test_an_unreadable_branch_still_fails_the_step_so_a_rerun_repeats_it(self):
        fx, r = self.decided()
        fx.fail("pulls_7")
        out = r.run(steps=[REAL])
        self.assertNotEqual(out[REAL].returncode, 0); self.assertIn("cannot read the PR's head branch", out[REAL].stderr)
        self.assertEqual(self.label_writes(fx), [])

    def test_no_prefix_means_no_scope(self):
        fx = findings_fixture()
        r = Runner(fx, unlock=True, HANDOFF_MODE="label")
        fx.put("contents_bridge-gate_config.json", {k: v for k, v in {**g.CONFIG, "real_handoff_enabled": True}.items() if k != "handoff_branch_prefix"})
        r.run(steps=[COLLECT, DECIDE, GUARD])
        self.assertEqual(r.run(steps=[REAL])[REAL].returncode, 0)
        self.assertEqual(len(self.label_writes(fx)), 1)


class ScopeWorkflowTests(unittest.TestCase):
    """The workflow's real steps, real mode, both locks open: only the scoped branch ever gets a label."""

    def test_a_pull_request_outside_the_scope_gets_no_label_and_no_attempt(self):
        c = Chain()
        c.set_head(1, "failure", ref="claude/e2e-synthetic-c")
        d, labels, notes = c.ci(1)
        self.assertEqual((d["decision"], d["reason"], labels), ("WAIT", "HANDOFF_OUT_OF_SCOPE", []))
        self.assertEqual(len(notes), 1)                                        # the state comment only: no notice, no label
        self.assertEqual(c.state["attempts"], [])
        self.assertEqual(c.state["last"]["reason"], "HANDOFF_OUT_OF_SCOPE")

    def test_the_scoped_branch_runs_the_whole_retry_cycle(self):
        c = Chain()
        c.set_head(1, "failure", ref=PREFIX + "a")
        d, labels, _ = c.ci(1)
        self.assertEqual((d["decision"], d["handoff"]["attempt"], labels), ("NEEDS_FIX", 1, [ADD]))


if __name__ == "__main__":
    unittest.main()
