"""The real handoff can be scoped to one family of disposable branches, so a live test does not expose the whole repository.

`handoff_branch_prefix` in the default branch's bridge-gate/config.json narrows where a label may ever be applied: a pull request whose head branch
does not start with it gets `WAIT / HANDOFF_OUT_OF_SCOPE`, no attempt and no label. It only narrows: the attempt limit, the lease, the lock and the
human-only merge rule are untouched, and simulate mode behaves exactly as before. No network, no GitHub, no AI, no Routine.
"""

import unittest

from . import gate_sim as g
from .gate_sim import run_gate
from .test_bridge_gate_codex_findings import check, clean, with_finding
from .test_bridge_gate_collect import Fixture
from .test_bridge_gate_retry_chain import ADD, Chain

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
        self.assertIs(g.CONFIG["real_handoff_enabled"], False)
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
