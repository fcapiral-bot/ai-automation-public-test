"""The automatic retry chain, executed through the workflow's REAL shell steps against a fake `gh`, one head after another.

The pure-gate tests (test_bridge_gate*.py) prove each decision; this module proves the label cycle the Routine depends on, across attempts:
a handoff label is added for attempt N, removed when the correction supersedes it, added again for attempt N+1, and never added a fourth
time. It also pins what does NOT retry (a session that failed: the head times out and stays blocked). No network, no GitHub, no AI, no
Routine. Whether a label applied by the workflow's own token starts the Routine is NOT tested here: that needs a live run.
"""

import json
import re
import unittest

from . import gate_sim as g
from .gate_sim import CODEX, sha
from .test_bridge_gate_collect import BOT, Fixture, state_comment_body, user
from .test_bridge_gate_workflow import COLLECT, DECIDE, GUARD, NOTIFY, PERSIST, REAL, REMOVE, SIMULATED, SUMMARY, Runner

REAL_PIPELINE = [COLLECT, DECIDE, GUARD, REMOVE, NOTIFY, PERSIST, REAL, SUMMARY]
SIM_PIPELINE = [COLLECT, DECIDE, GUARD, NOTIFY, PERSIST, SIMULATED, SUMMARY]
ADD = "POST labels labels[]=" + g.CONFIG["labels"]["needs_fix"]
REMOVE_W = "DELETE labels/" + g.CONFIG["labels"]["needs_fix"].replace(":", "%3A")


class Chain:
    """One PR whose head moves. Each `run` is one workflow run; the state comment it writes is fed to the next one, like GitHub would."""

    def __init__(self, real=True):
        self.real = real
        self.fx = Fixture(sha(1))
        self.runner = Runner(self.fx, unlock=real, HANDOFF_MODE="label" if real else "")
        self.state = None
        self.now = g.NOW
        self.seen_writes = 0

    def set_head(self, n, ci, findings=False, commit=None):
        """Head n; `ci` in failure/pending/success/None; Codex findings on every head up to n when `findings`; `commit` = (subject, parents)."""
        head = sha(n)
        fx = self.fx
        fx.put("pulls_7", {"number": 7, "state": "open", "merged": False, "draft": False, "title": "t",
                           "head": {"sha": head, "repo": {"full_name": "o/r"}}, "base": {"repo": {"full_name": "o/r"}},
                           "labels": [{"name": g.CONFIG["labels"]["needs_fix"], "id": 2}]})
        revs = [{"id": 100 + k, "user": user(CODEX), "state": "COMMENTED", "commit_id": sha(k)} for k in range(1, n + 1) if k < n or findings]
        fx.put("pulls_7_reviews", revs)
        fx.put("pulls_7_comments", [{"pull_request_review_id": x["id"], "in_reply_to_id": None} for x in revs])
        runs = [] if ci is None else [{"name": "unit-tests", "status": "in_progress" if ci == "pending" else "completed",
                                       "conclusion": None if ci == "pending" else ci, "check_suite": {"id": 100}, "app": {"id": 15368}}]
        fx.put("commits_SHA_check-runs", {"total_count": len(runs), "check_runs": runs})
        subject, parents = commit or ("work in progress", [])
        fx.put("commits_SHA", {"sha": head, "commit": {"message": subject}, "parents": [{"sha": p} for p in parents]})
        fx.put("commits_SHA_status", {"state": "failure" if ci == "failure" else "pending", "statuses": [], "sha": head, "total_count": 0})
        fx.put("issues_7_comments", [{"id": 555, "user": BOT, "body": state_comment_body(self.state)}] if self.state else [])
        self.head_n = n

    def fix_commit(self, attempt, parent):
        return ("Address review findings (bridge attempt %d/3)" % attempt, [sha(parent)])

    def run(self, event, **env):
        """One workflow run for `event`. The collect step derives the event key itself from the event's own fields (EVENT_KEY is overwritten),
        so those fields are what is set here: HEAD_SHA for a push, CI_RUN_ID/CI_RUN_ATTEMPT for `ci` completing, REVIEW_ID/REVIEW_ACTION for a
        review. Returns (decision, label writes of this run, notices posted by this run); `self.key` is the key the workflow really derived."""
        out = self.runner.run(REAL_PIPELINE if self.real else SIM_PIPELINE, EVENT_NAME=event, HEAD_SHA=sha(self.head_n), NOW=str(self.now), **env)
        failed = {k: v.stderr[-300:] for k, v in out.items() if v.returncode}
        assert not failed, failed
        self.key = json.loads((self.runner.work / "facts.json").read_text())["event"]["key"]
        writes = self.fx.writes()
        new = writes[self.seen_writes:]
        self.seen_writes = len(writes)
        bodies = sorted(self.fx.dir.glob("body-*.txt"), key=lambda q: int(q.stem.split("-")[1]))
        for p in bodies:
            m = re.match(r"<!-- bridge-gate-state:v1 (.*?) -->", p.read_text())
            if m:
                self.state = json.loads(m.group(1))
        self.fx.put("issues_7_comments", [{"id": 555, "user": BOT, "body": state_comment_body(self.state)}] if self.state else [])
        labels = [w.split(" ", 2)[0] + " " + w.split("/issues/7/", 1)[1] if w.startswith("DELETE") else w.split(" ", 2)[0] + " labels labels[]=" + w.rsplit("labels[]=", 1)[1]
                  for w in new if "/labels" in w]
        notes = [w for w in new if "/comments" in w and w.startswith("POST")]
        return self.runner.decision(), labels, notes

    def pushed(self):
        return self.run("pull_request")                                           # key: head:<sha>

    def ci(self, run_id, attempt=1):
        return self.run("workflow_run", CI_RUN_ID=str(run_id), CI_RUN_ATTEMPT=str(attempt))   # key: ci:<run>:<attempt>

    def review(self, review_id, action="submitted"):
        return self.run("pull_request_review", REVIEW_ID=str(review_id), REVIEW_ACTION=action)  # key: review:<id>

    def dispatched(self):
        return self.run("workflow_dispatch")                                      # no key: a manual or watchdog evaluation

    def statuses(self):
        return [(a["n"], a["status"]) for a in self.state["attempts"]]


class RetryChainTests(unittest.TestCase):
    def failing_chain(self):
        """Head 1 fails CI; each correction is pushed (the session's exact titled commit) and CI fails again."""
        c = Chain()
        c.set_head(1, "failure")
        c.ci(1)
        return c

    def test_every_failed_correction_is_retried_with_a_label_cycle_until_the_third_attempt(self):
        c = Chain()
        c.set_head(1, "failure")
        d, labels, _ = c.ci(1)
        self.assertEqual((d["decision"], d["handoff"]["attempt"], labels), ("NEEDS_FIX", 1, [ADD]))
        for n in (2, 3):
            fix = c.fix_commit(n - 1, n - 1)
            c.set_head(n, "pending", commit=fix)
            d, labels, _ = c.pushed()                                  # the push: attempt n-1 superseded, label released
            self.assertEqual((d["decision"], d["reason"], labels), ("WAIT", "CI_PENDING", [REMOVE_W]), n)
            c.set_head(n, "failure", commit=fix)
            d, labels, _ = c.ci(100 + n)                    # CI fails again: the next attempt and a fresh label
            self.assertEqual((d["decision"], d["handoff"]["attempt"], labels), ("NEEDS_FIX", n, [ADD]), n)
        self.assertEqual(c.statuses(), [(1, "superseded"), (2, "superseded"), (3, "in_flight")])

    def test_the_fourth_head_is_never_handed_off_and_the_cap_is_announced_once(self):
        c = self.failing_chain()
        for n in (2, 3):
            fix = c.fix_commit(n - 1, n - 1)
            c.set_head(n, "pending", commit=fix); c.pushed()
            c.set_head(n, "failure", commit=fix); c.ci(100 + n)
        fix = c.fix_commit(3, 3)
        c.set_head(4, "pending", commit=fix)
        d, labels, _ = c.pushed()
        self.assertEqual(labels, [REMOVE_W])                                       # attempt 3 is released like the others
        c.set_head(4, "failure", findings=True, commit=fix)
        d, labels, notes = c.ci(4)
        self.assertEqual((d["decision"], d["reason"], d["handoff"], labels), ("MAX_ATTEMPTS", "ATTEMPT_LIMIT", None, []))
        self.assertTrue(d["notify"])
        self.assertEqual(len(notes), 1)                                           # the one MAX_ATTEMPTS notice (the state is edited in place)
        self.assertEqual(c.statuses(), [(1, "superseded"), (2, "superseded"), (3, "superseded")])
        for event in (lambda: c.review(204), lambda: c.ci(9004), lambda: c.pushed()):
            d, labels, notes = event()
            self.assertEqual((d["decision"], labels, notes), ("MAX_ATTEMPTS", [], []), c.key)   # later events: no label, no second notice

    def test_when_the_failing_ci_result_arrives_before_the_push_event_the_label_is_still_released_then_added_in_that_order(self):
        c = self.failing_chain()
        fix = c.fix_commit(1, 1)
        c.set_head(2, "failure", commit=fix)
        d, labels, _ = c.ci(2)                               # no CI_PENDING evaluation in between
        self.assertEqual((d["decision"], d["handoff"]["attempt"], labels), ("NEEDS_FIX", 2, [REMOVE_W, ADD]))
        self.assertEqual(c.statuses(), [(1, "superseded"), (2, "in_flight")])

    def test_codex_findings_without_a_ci_failure_retry_the_same_way(self):
        c = Chain()
        c.set_head(1, "success", findings=True)
        d, labels, _ = c.review(101)
        self.assertEqual((d["decision"], d["reason"], labels), ("NEEDS_FIX", "CODEX_FINDINGS", [ADD]))
        fix = c.fix_commit(1, 1)
        c.set_head(2, "success", findings=True, commit=fix)
        d, labels, _ = c.review(102)                 # the new head's review still has findings
        self.assertEqual((d["decision"], d["handoff"]["attempt"], labels), ("NEEDS_FIX", 2, [REMOVE_W, ADD]))

    def test_a_correction_that_leaves_the_pr_clean_ends_the_loop_without_another_label(self):
        c = self.failing_chain()
        fix = c.fix_commit(1, 1)
        c.set_head(2, "success", commit=fix)
        d, labels, _ = c.ci(2)
        self.assertEqual((d["decision"], labels, c.statuses()), ("WAIT", [REMOVE_W], [(1, "superseded")]))   # waiting for Codex, nothing re-added

    def test_a_session_that_never_pushed_is_not_retried_on_the_same_head_but_a_new_head_is_and_the_timeout_counts(self):
        """Design, not a defect: a timeout never retries by itself. It ends with a label release and one notice, and the head stays blocked."""
        c = self.failing_chain()
        c.now += 91 * 60                                                          # the lease ran out; the watchdog's dispatch evaluates it
        d, labels, notes = c.dispatched()
        self.assertEqual((d["decision"], d["reason"], labels, d["notify"]), ("BLOCKED", "HANDOFF_TIMEOUT", [REMOVE_W], True))
        self.assertEqual(len(notes), 1)                                           # one notice
        for event in (lambda: c.ci(9001), lambda: c.review(101)):
            c.set_head(1, "failure", findings=True)
            d, labels, notes = event()
            self.assertEqual((d["decision"], d["handoff"], labels, notes), ("BLOCKED", None, [], []), c.key)
        c.set_head(2, "failure", commit=("fix typo", [sha(1)]))                    # a person pushes: a fresh head, a new attempt
        d, labels, _ = c.pushed()
        self.assertEqual((d["decision"], d["handoff"]["attempt"], labels), ("NEEDS_FIX", 2, [ADD]))
        self.assertEqual(c.statuses(), [(1, "timed_out"), (2, "in_flight")])

    def test_timeouts_use_up_attempts_exactly_like_corrections(self):
        c = self.failing_chain()
        for n in (2, 3):
            c.now += 91 * 60
            c.dispatched()
            c.set_head(n, "failure", commit=("fix typo", [sha(n - 1)]))
            d, labels, _ = c.pushed()
            self.assertEqual((d["decision"], d["handoff"]["attempt"]), ("NEEDS_FIX", n))
        c.now += 91 * 60
        c.dispatched()
        c.set_head(4, "failure", commit=("fix typo", [sha(3)]))
        d, labels, _ = c.pushed()
        self.assertEqual((d["decision"], d["reason"], d["handoff"], labels), ("MAX_ATTEMPTS", "ATTEMPT_LIMIT", None, []))
        self.assertEqual(c.statuses(), [(1, "timed_out"), (2, "timed_out"), (3, "timed_out")])

    def test_the_events_carry_the_workflows_real_keys_and_a_redelivered_event_never_hands_off_twice(self):
        """Codex finding on e051195: the collect step derives each key from the event's own fields, so the helpers set those fields."""
        c = Chain()
        c.set_head(1, "failure")
        d, labels, _ = c.ci(101)
        self.assertEqual((c.key, d["duplicate"], labels), ("ci:101:1", False, [ADD]))
        d, labels, notes = c.ci(101)                                               # the same CI completion delivered again
        self.assertEqual((c.key, d["duplicate"], d["reason"], labels, notes), ("ci:101:1", True, "HANDOFF_IN_FLIGHT", [], []))
        d, labels, _ = c.ci(101, attempt=2)                                        # a re-run of that job is a different event, still no second attempt
        self.assertEqual((c.key, d["duplicate"], d["reason"], labels), ("ci:101:2", False, "HANDOFF_IN_FLIGHT", []))
        d, labels, _ = c.ci(102)
        self.assertEqual((c.key, d["duplicate"], labels), ("ci:102:1", False, []))
        d, labels, _ = c.review(204)
        self.assertEqual((c.key, labels), ("review:204", []))
        d, labels, _ = c.review(204, action="dismissed")
        self.assertEqual(c.key, "review:204:dismissed")
        d, labels, _ = c.pushed()
        self.assertEqual((c.key, labels), ("head:" + sha(1), []))
        d, labels, _ = c.dispatched()
        self.assertEqual((c.key, labels), (None, []))                              # no key: a manual or watchdog evaluation is never a duplicate
        self.assertEqual(c.statuses(), [(1, "in_flight")])
        self.assertEqual(len(c.state["seen"]), len(set(c.state["seen"])))          # each distinct event recorded once

    def test_in_simulate_mode_the_same_chain_never_touches_a_label(self):
        c = Chain(real=False)
        c.set_head(1, "failure")
        c.ci(1)
        for n in (2, 3, 4):
            fix = c.fix_commit(n - 1, n - 1)
            c.set_head(n, "pending", commit=fix); c.pushed()
            c.set_head(n, "failure", commit=fix); c.ci(100 + n)
        self.assertEqual([w for w in c.fx.writes() if "/labels" in w], [])
        self.assertEqual(c.statuses(), [(1, "superseded"), (2, "superseded"), (3, "superseded")])   # the cap holds in simulation too


if __name__ == "__main__":
    unittest.main()
