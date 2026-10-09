"""Dismissed reviews wake the gate (Codex finding "Re-evaluate dismissed reviews" on PR #1, 832bb3b; valid).

When a human's CHANGES_REQUESTED review is dismissed, GitHub emits `pull_request_review` with action `dismissed`. The trigger accepted
only `submitted`, so the gate stayed BLOCKED / HUMAN_CHANGES_REQUESTED until some unrelated event. Now `dismissed` runs the gate, with its
own event key (a submitted event keeps the plain `review:<id>` key), so a dismissal is never mistaken for a duplicate of the submission.
No network, no AI: the real workflow steps run against a fake GitHub.
"""

import json
import re
import unittest

from . import gate_sim as g
from .gate_sim import CODEX, HUMAN, NOW, sha, summary
from .test_bridge_gate_collect import BOT, Fixture, state_comment_body, user
from .test_bridge_gate_workflow import CODE, COLLECT, DECIDE, Runner, SIMULATED, script_of, step_blocks

H = sha(1)
OTHER = {"login": "hubot-reviewer", "uid": 777, "type": "User"}
SUBMITTED = {"REVIEW_ID": "201", "REVIEW_ACTION": "submitted"}
DISMISSED = {"REVIEW_ID": "201", "REVIEW_ACTION": "dismissed"}


def world(reviews, state=None, codex_clean=True, finding=False, ci=True):
    fx = Fixture(H)
    revs = [{"id": rid, "user": user(who), "state": st, "commit_id": H} for rid, who, st in reviews]
    comments = []
    if finding:                                                     # a real Codex finding on the current head
        revs.append({"id": 101, "user": user(CODEX), "state": "COMMENTED", "commit_id": H})
        fx.put("pulls_7_comments", [{"pull_request_review_id": 101, "in_reply_to_id": None}])
    fx.put("pulls_7_reviews", revs)
    if codex_clean:
        comments.append({"id": 9000, "user": user(CODEX), "body": summary(H)["body"]})
    if state is not None:
        comments.append({"id": 4242, "user": BOT, "body": state_comment_body(state)})
    fx.put("issues_7_comments", comments)
    fx.put("commits_SHA_check-runs", {"total_count": 1, "check_runs": [
        {"name": "unit-tests", "status": "completed", "conclusion": "success" if ci else "failure", "check_suite": {"id": 100}, "app": {"id": 15368}}]})
    return fx


def saved(runner):
    body = next(b.read_text() for b in runner.bodies() if b.read_text().startswith("<!-- bridge-gate-state:v1 "))
    return json.loads(body.splitlines()[0][len("<!-- bridge-gate-state:v1 "):-len(" -->")])


def notices(runner):
    return [b.read_text() for b in runner.bodies() if b.read_text().startswith("<!-- bridge-gate-notice:v1 ")]


def run_ok(runner, **env):
    out = runner.run(**env)
    bad = {k: v.stderr for k, v in out.items() if v.returncode != 0}
    assert not bad, bad
    return out


def event_key(runner, **env):
    out = runner.run(steps=[COLLECT], **env)
    assert out[COLLECT].returncode == 0, out[COLLECT].stderr
    return json.loads((runner.work / "facts.json").read_text())["event"]["key"]


def attempt(n, head, status="superseded"):
    return {"n": n, "head": head, "trigger": "x", "reason": "CODEX_FINDINGS", "at": NOW - 10_000, "lease_expires": NOW - 5_000, "status": status}


class TriggerTests(unittest.TestCase):
    def test_the_review_trigger_is_exactly_submitted_and_dismissed(self):
        on_block = CODE.split("\non:", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("pull_request_review:\n    types: [submitted, dismissed]", on_block)
        review_types = re.search(r"pull_request_review:\n    types: \[([^\]]*)\]", on_block).group(1)
        self.assertEqual(review_types, "submitted, dismissed")                          # `edited` is not a trigger: a body edit does not change a review's state
        self.assertEqual(re.findall(r"^  ([a-z_]+):", on_block, re.M),
                         ["pull_request", "pull_request_review", "issue_comment", "workflow_run", "workflow_dispatch"])   # no new event

    def test_permissions_secrets_and_scripts_did_not_grow(self):
        block = re.search(r"(?m)^permissions:\n((?:  .*\n)+)", CODE).group(1)
        self.assertEqual(dict(re.findall(r"^  ([a-z-]+):\s*(\S+)", block, re.M)),
                         {"contents": "read", "pull-requests": "write", "checks": "read", "statuses": "read", "actions": "read"})
        self.assertEqual(set(re.findall(r"secrets\.(\w+)", CODE)), {"GITHUB_TOKEN"})
        self.assertIn("REVIEW_ACTION: ${{ github.event.action }}", step_blocks()["Collect evidence (read-only)"])   # via env, never in a script
        for name, block in step_blocks().items():
            if "run:" in block:
                self.assertNotIn("${{", script_of(name), name)
        self.assertNotRegex(CODE, r"(?m)^\s*schedule:|cron:")                         # still no watchdog


class EventKeyTests(unittest.TestCase):
    def runner(self):
        return Runner(world([(201, HUMAN, "CHANGES_REQUESTED")]))

    def test_a_submitted_review_keeps_its_plain_key_so_existing_history_still_deduplicates(self):
        self.assertEqual(event_key(self.runner(), **SUBMITTED), "review:201")

    def test_a_dismissal_has_its_own_key_distinct_from_the_submission(self):
        r = self.runner()
        self.assertEqual(event_key(r, **DISMISSED), "review:201:dismissed")
        self.assertNotEqual(event_key(r, **DISMISSED), event_key(r, **SUBMITTED))

    def test_an_invalid_action_gives_no_key_and_executes_nothing(self):
        for bad in ("", "Dismissed", "a b", "x;touch pwned", "$(touch pwned)", "`touch pwned`", "dismissed\ntouch pwned"):
            r = self.runner()
            self.assertIsNone(event_key(r, REVIEW_ID="201", REVIEW_ACTION=bad), repr(bad))
            self.assertFalse((r.work / "pwned").exists(), repr(bad))

    def test_other_events_keep_their_keys(self):
        r = self.runner()
        self.assertEqual(event_key(r, EVENT_NAME="pull_request", HEAD_SHA=H), "head:" + H)
        self.assertEqual(event_key(r, EVENT_NAME="issue_comment", COMMENT_ID="5", COMMENT_UPDATED="t"), "comment:5@t")
        self.assertEqual(event_key(r, EVENT_NAME="workflow_run", CI_RUN_ID="9", CI_RUN_ATTEMPT="1"), "ci:9:1")


class DismissalFlowTests(unittest.TestCase):
    def blocked(self):
        """The human requested changes; the submitted event was handled and the gate recorded BLOCKED."""
        fx = world([(201, HUMAN, "CHANGES_REQUESTED")])
        r = Runner(fx, **SUBMITTED); run_ok(r)
        self.assertEqual((r.decision()["decision"], r.decision()["reason"]), ("BLOCKED", "HUMAN_CHANGES_REQUESTED"))
        return saved(r)

    def test_dismissing_the_blocking_review_causes_a_fresh_evaluation_that_resumes_the_flow(self):
        state = self.blocked()
        fx = world([(201, HUMAN, "DISMISSED")], state=state)
        r = Runner(fx, **DISMISSED); run_ok(r)
        d = r.decision()
        self.assertEqual((d["decision"], d["reason"], d["duplicate"], d["notify"]), ("READY", "CODEX_CLEAN_AND_CI_PASSING", False, True))
        self.assertEqual(saved(r)["last"]["decision"], "READY")
        self.assertEqual([w.split()[0] for w in fx.writes()], ["POST", "PATCH"])          # notice, then the state edited in place
        self.assertIn("READY for human review", notices(r)[0])

    def test_the_same_dismissal_delivered_again_is_a_duplicate_and_changes_nothing(self):
        state = self.blocked()
        fx = world([(201, HUMAN, "DISMISSED")], state=state)
        r = Runner(fx, **DISMISSED); run_ok(r)
        settled = saved(r)
        for _ in range(3):
            fx2 = world([(201, HUMAN, "DISMISSED")], state=settled)
            r2 = Runner(fx2, **DISMISSED); run_ok(r2)
            self.assertTrue(r2.decision()["duplicate"])
            self.assertEqual(fx2.writes(), [])

    def test_another_humans_block_survives_a_dismissal(self):
        fx = world([(201, HUMAN, "DISMISSED"), (202, OTHER, "CHANGES_REQUESTED")], state=self.blocked())
        r = Runner(fx, **DISMISSED); run_ok(r)
        self.assertEqual((r.decision()["decision"], r.decision()["reason"]), ("BLOCKED", "HUMAN_CHANGES_REQUESTED"))

    def test_a_dismissal_with_a_pending_codex_finding_hands_off_exactly_once(self):
        state = self.blocked()
        fx = world([(201, HUMAN, "DISMISSED")], state=state, codex_clean=False, finding=True)
        r = Runner(fx, **DISMISSED); out = run_ok(r)
        d = r.decision()
        self.assertEqual((d["decision"], d["reason"], d["handoff"]["attempt"], d["handoff"]["mode"]), ("NEEDS_FIX", "CODEX_FINDINGS", 1, "simulate"))
        self.assertEqual(out[SIMULATED].stdout.count("SIMULATED HANDOFF"), 1)
        settled = saved(r)
        for _ in range(3):                                                            # the dismissal event arrives again and again
            fx2 = world([(201, HUMAN, "DISMISSED")], state=settled, codex_clean=False, finding=True)
            r2 = Runner(fx2, **DISMISSED); out2 = run_ok(r2)
            d2 = r2.decision()
            self.assertEqual((d2["decision"], d2["reason"], d2["handoff"]), ("WAIT", "HANDOFF_IN_FLIGHT", None))
            self.assertEqual(len(d2["state"]["attempts"]), 1)
            self.assertNotIn("SIMULATED HANDOFF", out2[SIMULATED].stdout)

    def test_the_attempt_limit_and_history_hold_across_a_dismissal(self):
        used = {"v": 1, "attempts": [attempt(1, sha(11)), attempt(2, sha(12)), attempt(3, sha(13))], "seen": ["review:201"], "last": None}
        fx = world([(201, HUMAN, "DISMISSED")], state=used, codex_clean=False, finding=True)
        r = Runner(fx, **DISMISSED); out = run_ok(r)
        d = r.decision()
        self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("MAX_ATTEMPTS", "ATTEMPT_LIMIT", None))
        self.assertEqual(d["state"]["attempts"], used["attempts"])                    # history byte-for-byte unchanged
        self.assertNotIn("SIMULATED HANDOFF", out[SIMULATED].stdout)

    def test_a_dismissal_never_rewrites_existing_attempts(self):
        live = {"v": 1, "attempts": [attempt(1, sha(11)), attempt(2, H, "in_flight") | {"lease_expires": NOW + 3000}], "seen": ["review:201"], "last": None}
        fx = world([(201, HUMAN, "DISMISSED")], state=live)
        r = Runner(fx, **DISMISSED); run_ok(r)
        self.assertEqual(r.decision()["state"]["attempts"], live["attempts"])

    def test_a_head_that_moves_before_the_writes_is_refused_for_a_dismissal_too(self):
        fx = world([(201, HUMAN, "DISMISSED")], state=self.blocked())
        r = Runner(fx, **DISMISSED); r.run(steps=[COLLECT, DECIDE])
        pr = json.loads((fx.dir / "pulls_7.json").read_text()); pr["head"]["sha"] = sha(2); fx.put("pulls_7", pr)
        out = r.run(steps=["Guard (head unchanged)", "Notify on READY, BLOCKED, MAX_ATTEMPTS or USAGE_STOP", "Persist state"])
        self.assertEqual(list(out), ["Guard (head unchanged)"]); self.assertEqual(fx.writes(), [])


class SubmittedReviewsStillWorkTests(unittest.TestCase):
    def test_a_submitted_changes_requested_review_blocks(self):
        r = Runner(world([(201, HUMAN, "CHANGES_REQUESTED")]), **SUBMITTED); run_ok(r)
        self.assertEqual((r.decision()["decision"], r.decision()["reason"]), ("BLOCKED", "HUMAN_CHANGES_REQUESTED"))
        self.assertIn("HUMAN_CHANGES_REQUESTED", notices(r)[0])

    def test_a_submitted_approval_does_not_block(self):
        r = Runner(world([(201, HUMAN, "APPROVED")]), **SUBMITTED); run_ok(r)
        self.assertEqual(r.decision()["decision"], "READY")

    def test_a_submitted_codex_finding_still_requests_one_simulated_fix(self):
        fx = world([], codex_clean=False, finding=True)
        r = Runner(fx, REVIEW_ID="101", REVIEW_ACTION="submitted"); out = run_ok(r)
        d = r.decision()
        self.assertEqual((d["decision"], d["handoff"]["attempt"], d["handoff"]["of"]), ("NEEDS_FIX", 1, 3))
        self.assertEqual(out[SIMULATED].stdout.count("SIMULATED HANDOFF"), 1)
        self.assertEqual(json.loads((r.work / "facts.json").read_text())["event"]["key"], "review:101")


class DocumentationTests(unittest.TestCase):
    def test_the_trigger_and_the_finding_are_documented(self):
        readme = re.sub(r"\s+", " ", (g.GATE / "README.md").read_text())
        self.assertRegex(readme, r"submitted or dismissed")
        self.assertIn("review:<id>:dismissed", readme)
        sec = re.sub(r"\s+", " ", (g.GATE / "SECURITY.md").read_text())
        self.assertRegex(sec, r"\| S16 \| Low \| \*\*Dismissed reviews did not wake the gate\*\*.*\*\*Fixed\.\*\*")

    def test_the_safeguards_and_scope_are_unchanged(self):
        cfg = json.loads((g.GATE / "config.json").read_text())
        self.assertIs(cfg["real_handoff_enabled"], False); self.assertIs(cfg["require_ci"], True); self.assertEqual(cfg["max_attempts"], 3)
        sec = re.sub(r"\s+", " ", (g.GATE / "SECURITY.md").read_text())
        self.assertRegex(sec, r"\| S15 \| \*\*High for unattended activation\*\*.*\*\*Open\. Documented blocker for unattended activation\.\*\*")


if __name__ == "__main__":
    unittest.main()
