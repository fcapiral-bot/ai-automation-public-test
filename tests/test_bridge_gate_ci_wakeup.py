"""The gate must re-evaluate when the `ci` workflow completes (a `workflow_run` event), safely.

Scenario: Codex finishes while CI is still running, the gate decides WAIT / CI_PENDING, then CI finishes and no other
event ever arrives. Without a CI-completion trigger the PR would sit there forever. These tests run the workflow's REAL
shell steps against a fake GitHub (no network, no AI) and pin what the new trigger may and may not touch.
"""

import json
import re
import unittest

from . import gate_sim as g
from .gate_sim import CODEX, review, sha, summary
from .test_bridge_gate_collect import BOT, Fixture, state_comment_body, user
from .test_bridge_gate_workflow import (CODE, COLLECT, DECIDE, NOTIFY, PERSIST, PIPELINE, SIMULATED, Runner, script_of,
                                        step_blocks)

CI_YML = (g.ROOT / ".github" / "workflows" / "ci.yml").read_text()
HEAD = sha(1)
RUN = {"EVENT_NAME": "workflow_run", "CI_RUN_ID": "555", "CI_RUN_ATTEMPT": "1"}
CODEX_EVENT = {"EVENT_NAME": "issue_comment", "COMMENT_ID": "9000", "COMMENT_UPDATED": "2026-10-09T17:23:52Z"}


def set_ci(fx, status, conclusion=None):
    fx.put("commits_SHA_check-runs", {"total_count": 1, "check_runs": [
        {"name": "unit-tests", "status": status, "conclusion": conclusion, "check_suite": {"id": 100}, "app": {"id": 15368}}]})


def world(head=HEAD, status="completed", conclusion="success", state=None, codex_clean=True, other_commit=None):
    """A PR whose Codex review of `head` is complete and clean, with CI in the given state."""
    fx = Fixture(head)
    comments = []
    if codex_clean:
        comments.append({"id": 9000, "user": user(CODEX), "body": summary(head, other_commit=other_commit)["body"]})
    if state is not None:
        comments.append({"id": 4242, "user": BOT, "body": state_comment_body(state)})
    fx.put("issues_7_comments", comments)
    set_ci(fx, status, conclusion)
    return fx


def attempt(n, head, status="superseded"):
    return {"n": n, "head": head, "trigger": "x", "reason": "CI_FAILING", "at": g.NOW - 10_000,
            "lease_expires": g.NOW - 5_000, "status": status}


def persisted_state(runner):
    first = runner.bodies()[0].read_text().splitlines()[0]
    return json.loads(first[len("<!-- bridge-gate-state:v1 "):-len(" -->")])


def run_ok(runner, **env):
    out = runner.run(**env)
    bad = {k: v.stderr for k, v in out.items() if v.returncode != 0}
    assert not bad, bad
    return out


class RaceTests(unittest.TestCase):
    def test_codex_done_while_ci_runs_then_ci_finishing_wakes_the_gate_to_ready(self):
        # 1. Codex finishes first; CI is still running: the gate waits.
        fx = world(status="in_progress", conclusion=None)
        r1 = Runner(fx)
        run_ok(r1, **CODEX_EVENT)
        self.assertEqual((r1.decision()["decision"], r1.decision()["reason"]), ("WAIT", "CI_PENDING"))
        saved = persisted_state(r1)
        # 2. CI finishes. The ONLY new event is the workflow_run completion.
        fx2 = world(state=saved)
        r2 = Runner(fx2)
        out = run_ok(r2, **RUN)
        d = r2.decision()
        self.assertEqual((d["decision"], d["reason"], d["duplicate"], d["handoff"]), ("READY", "CODEX_CLEAN_AND_CI_PASSING", False, None))
        self.assertEqual([w.split()[0] for w in fx2.writes()], ["PATCH", "POST"])  # state edited in place, one notification
        self.assertIn("PATCH repos/o/r/issues/comments/4242", fx2.writes()[0])
        self.assertIn("READY for human review", r2.bodies()[1].read_text())
        self.assertNotIn("SIMULATED HANDOFF", out[SIMULATED].stdout)
        self.assertEqual([w for w in fx2.writes() if "labels" in w], [])

    def test_the_other_order_still_works_ci_first_then_codex(self):
        fx = world(codex_clean=False)                       # CI green, Codex has not reported
        r1 = Runner(fx)
        run_ok(r1, **RUN)
        self.assertEqual(r1.decision()["reason"], "AWAITING_CODEX_REVIEW")
        fx2 = world(state=persisted_state(r1))              # then Codex's summary appears (an issue_comment event)
        r2 = Runner(fx2)
        run_ok(r2, **CODEX_EVENT)
        self.assertEqual(r2.decision()["decision"], "READY")

    def test_ci_still_running_at_the_ci_event_keeps_waiting_without_writing_a_handoff(self):
        fx = world(status="in_progress", conclusion=None)
        r = Runner(fx)
        run_ok(r, **RUN)
        self.assertEqual((r.decision()["decision"], r.decision()["reason"]), ("WAIT", "CI_PENDING"))
        self.assertIsNone(r.decision()["handoff"])

    def test_a_failed_ci_run_requests_one_simulated_fix_and_only_a_comment_is_written(self):
        fx = world(status="completed", conclusion="failure")
        r = Runner(fx)
        out = run_ok(r, **RUN)
        d = r.decision()
        self.assertEqual((d["decision"], d["reason"]), ("NEEDS_FIX", "CI_FAILING"))
        self.assertEqual((d["handoff"]["attempt"], d["handoff"]["of"], d["handoff"]["mode"]), (1, 3, "simulate"))
        self.assertIn("SIMULATED HANDOFF", out[SIMULATED].stdout)
        self.assertEqual([w.split()[0] for w in fx.writes()], ["POST"])
        self.assertEqual([w for w in fx.writes() if "labels" in w], [])

    def test_the_same_ci_event_delivered_again_never_hands_off_twice(self):
        fx = world(conclusion="failure")
        r = Runner(fx)
        run_ok(r, **RUN)
        saved = persisted_state(r)
        for env in (RUN, {**RUN, "CI_RUN_ATTEMPT": "2"}, {**RUN, "CI_RUN_ID": "556"}):   # redelivery, CI re-run, another run
            fx2 = world(conclusion="failure", state=saved)
            r2 = Runner(fx2)
            out = run_ok(r2, **env)
            d = r2.decision()
            self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("WAIT", "HANDOFF_IN_FLIGHT", None), env)
            self.assertNotIn("SIMULATED HANDOFF", out[SIMULATED].stdout)
            self.assertEqual(len(d["state"]["attempts"]), 1)

    def test_an_exact_redelivery_is_recognised_as_a_duplicate(self):
        fx = world(conclusion="failure")
        r = Runner(fx)
        run_ok(r, **RUN)
        r2 = Runner(world(conclusion="failure", state=persisted_state(r)))
        run_ok(r2, **RUN)
        self.assertTrue(r2.decision()["duplicate"])

    def test_the_three_attempt_limit_holds_for_ci_failures(self):
        state = {"v": 1, "attempts": [attempt(1, sha(11)), attempt(2, sha(12)), attempt(3, sha(13))], "seen": [], "last": None}
        fx = world(conclusion="failure", state=state)
        r = Runner(fx)
        out = run_ok(r, **RUN)
        d = r.decision()
        self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("MAX_ATTEMPTS", "ATTEMPT_LIMIT", None))
        self.assertNotIn("SIMULATED HANDOFF", out[SIMULATED].stdout)
        self.assertEqual(len(d["state"]["attempts"]), 3)

    def test_a_draft_pr_never_gets_a_handoff_from_a_ci_event(self):
        fx = world(conclusion="failure")
        pr = json.loads((fx.dir / "pulls_7.json").read_text()); pr["draft"] = True; fx.put("pulls_7", pr)
        r = Runner(fx)
        run_ok(r, **RUN)
        self.assertEqual((r.decision()["decision"], r.decision()["reason"], r.decision()["handoff"]), ("WAIT", "DRAFT", None))

    def test_a_fork_pr_is_refused_even_if_a_ci_event_names_it(self):
        fx = world(conclusion="failure")
        pr = json.loads((fx.dir / "pulls_7.json").read_text()); pr["head"]["repo"]["full_name"] = "someone/fork"; fx.put("pulls_7", pr)
        r = Runner(fx)
        run_ok(r, **RUN)
        self.assertEqual((r.decision()["decision"], r.decision()["reason"], r.decision()["handoff"]), ("BLOCKED", "FORK_PR", None))

    def test_unreadable_ci_evidence_fails_closed_on_a_ci_event(self):
        fx = world()
        fx.fail("commits_SHA_check-runs")
        r = Runner(fx)
        out = run_ok(r, **RUN)
        self.assertEqual((r.decision()["decision"], r.decision()["reason"], r.decision()["handoff"]), ("BLOCKED", "EVIDENCE_UNAVAILABLE", None))
        self.assertEqual(fx.writes(), [])
        self.assertNotIn("SIMULATED HANDOFF", out[SIMULATED].stdout)


class StaleCiTests(unittest.TestCase):
    """The gate judges the PR's CURRENT head from the API. Nothing in the event can vouch for a different commit."""

    def test_a_green_run_for_an_older_head_cannot_unblock_a_newer_commit(self):
        new, old = sha(2), sha(1)
        fx = world(head=new, status="in_progress", conclusion=None, other_commit=old)  # new head: CI running; old head was green
        r = Runner(fx)
        run_ok(r, **RUN, HEAD_SHA=old)   # the event is about the old commit's CI run
        d = r.decision()
        self.assertEqual((d["decision"], d["reason"]), ("WAIT", "CI_PENDING"))
        self.assertEqual(d["head"], new)

    def test_a_newer_head_with_no_ci_yet_waits_it_is_never_ready(self):
        new = sha(2)
        fx = world(head=new, other_commit=sha(1))
        fx.put("commits_SHA_check-runs", {"total_count": 0, "check_runs": []})
        r = Runner(fx)
        run_ok(r, **RUN, HEAD_SHA=sha(1))
        self.assertEqual((r.decision()["decision"], r.decision()["reason"]), ("WAIT", "CI_MISSING"))

    def test_check_runs_are_only_ever_requested_for_the_current_head(self):
        new = sha(2)
        fx = world(head=new, other_commit=sha(1))
        r = Runner(fx)
        run_ok(r, **RUN, HEAD_SHA=sha(1))
        asked = [c for c in fx.calls() if "check-runs" in c or "/status" in c]
        self.assertTrue(asked)
        for call in asked:
            self.assertIn(new, call)
            self.assertNotIn(sha(1), call)

    def test_event_payload_values_never_reach_the_decision_or_the_collector(self):
        for name in ("collect.sh", "gate.jq", "guard.sh", "render.jq"):
            text = (g.GATE / name).read_text()
            for token in ("CI_RUN", "HEAD_SHA"):
                self.assertNotIn(token, text, (name, token))
            self.assertNotRegex(text, r"workflow_run(?!s)", name)   # `workflow_runs` is the Actions REST path, a different thing

    def test_a_cancelled_old_run_on_a_superseded_head_does_not_block_the_new_head(self):
        # CI concurrency cancels the older run when a newer push arrives; its completion event still wakes the gate.
        new = sha(2)
        fx = world(head=new, other_commit=sha(1))      # the NEW head's CI is green
        r = Runner(fx)
        run_ok(r, **RUN, HEAD_SHA=sha(1))              # event: the cancelled run for the old head
        self.assertEqual(r.decision()["decision"], "READY")


class MultiplePullRequestTests(unittest.TestCase):
    def test_two_active_prs_are_evaluated_and_written_independently(self):
        fx = Fixture(HEAD)
        set_ci(fx, "completed", "success")
        # PR 7 has a real Codex finding on its head; PR 9 is clean with a completed Codex review.
        fx.put("pulls_7_reviews", [{"id": 101, "user": user(CODEX), "state": "COMMENTED", "commit_id": HEAD}])
        fx.put("pulls_7_comments", [{"pull_request_review_id": 101, "in_reply_to_id": None}])
        pr9 = json.loads((fx.dir / "pulls_7.json").read_text()); pr9["number"] = 9
        fx.put("pulls_9", pr9)
        fx.put("pulls_9_reviews", [])
        fx.put("pulls_9_comments", [])
        fx.put("issues_9_comments", [{"id": 9000, "user": user(CODEX), "body": summary(HEAD)["body"]}])

        r7 = Runner(fx)
        run_ok(r7, **RUN)
        n7 = len(fx.writes())
        r9 = Runner(fx, PR_NUMBER="9")
        run_ok(r9, **{**RUN, "CI_RUN_ID": "556"})

        self.assertEqual((r7.decision()["decision"], r7.decision()["reason"]), ("NEEDS_FIX", "CODEX_FINDINGS"))
        self.assertEqual((r9.decision()["decision"], r9.decision()["reason"]), ("READY", "CODEX_CLEAN_AND_CI_PASSING"))
        w7, w9 = fx.writes()[:n7], fx.writes()[n7:]
        self.assertTrue(w7 and all("/issues/7/" in w for w in w7), w7)
        self.assertTrue(w9 and all("/issues/9/" in w for w in w9), w9)
        self.assertEqual(len(r7.decision()["state"]["attempts"]), 1)
        self.assertEqual(r9.decision()["state"]["attempts"], [])   # PR 9 inherited nothing from PR 7
        self.assertIsNone(r9.decision()["handoff"])

    def test_each_pr_gets_its_own_concurrency_group_and_unlinked_runs_never_share_one(self):
        group = re.search(r"(?m)^  group: (.+)$", CODE).group(1)
        self.assertIn("github.event.workflow_run.pull_requests[0].number", group)
        self.assertTrue(group.rstrip(" }").endswith("github.run_id"), group)   # an unlinked run gets a group of its own
        self.assertIn("cancel-in-progress: false", CODE)                      # a gate run is never cancelled midway


class CollectKeyTests(unittest.TestCase):
    def key(self, **env):
        r = Runner(world())
        run_ok_steps = r.run(steps=[COLLECT], **env)
        self.assertEqual(run_ok_steps[COLLECT].returncode, 0, run_ok_steps[COLLECT].stderr)
        return json.loads((r.work / "facts.json").read_text())["event"]["key"], r

    def test_the_event_key_is_unique_per_ci_run_and_attempt(self):
        self.assertEqual(self.key(**RUN)[0], "ci:555:1")
        self.assertEqual(self.key(**{**RUN, "CI_RUN_ATTEMPT": "2"})[0], "ci:555:2")

    def test_non_numeric_ids_give_no_key_and_execute_nothing(self):
        for bad in ("5;touch pwned", "$(touch pwned)", "`touch pwned`", "5 6", ""):
            key, r = self.key(**{**RUN, "CI_RUN_ID": bad})
            self.assertIsNone(key, bad)
            self.assertFalse((r.work / "pwned").exists(), bad)


class WorkflowStaticTests(unittest.TestCase):
    def test_exactly_one_workflow_run_trigger_naming_only_the_ci_workflow_on_completion(self):
        self.assertEqual(len(re.findall(r"(?m)^  workflow_run:", CODE)), 1)
        self.assertIn("workflow_run:\n    workflows: [ci]\n    types: [completed]", CODE)
        self.assertRegex(CI_YML, r"(?m)^name: ci$")            # the name the trigger matches
        self.assertNotIn("bridge-gate", re.search(r"workflows: \[(.*?)\]", CODE).group(1))   # it can never wake itself

    def test_the_ci_workflow_is_not_itself_chained_from_another_workflow(self):
        ci_code = "\n".join(l for l in CI_YML.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotRegex(ci_code, r"workflow_run")           # chain depth stays at one

    def test_pr_code_is_never_checked_out_in_the_privileged_path(self):
        block = step_blocks()["actions/checkout@v4"]
        self.assertNotRegex(block, r"(?m)^\s+(ref|repository|token|path):")
        self.assertIn("persist-credentials: false", block)
        self.assertNotRegex(CODE, r"head_sha|head_branch|head_commit|head\.ref|\.head_repository\.(?!full_name)")

    def test_only_numeric_ids_and_the_pr_number_are_taken_from_the_event(self):
        used = set(re.findall(r"github\.event\.workflow_run\.([A-Za-z_.\[\]0-9]+)", CODE))
        self.assertEqual(used, {"pull_requests[0].number", "event", "head_repository.full_name", "id", "run_attempt"})
        for name, block in step_blocks().items():
            if "run:" in block:
                self.assertNotIn("${{", script_of(name), name)  # event data goes through env, never into a script

    def test_the_job_only_proceeds_for_a_same_repo_pull_request_run_linked_to_a_pr(self):
        cond = re.search(r"(?s)    if: >-\n(.*?)\n    runs-on:", CODE).group(1)
        for needle in ("github.event_name != 'workflow_run'", "github.event.workflow_run.event == 'pull_request'",
                       "github.event.workflow_run.head_repository.full_name == github.repository",
                       "github.event.workflow_run.pull_requests[0].number != null"):
            self.assertIn(needle, cond)

    def test_permissions_and_secrets_did_not_grow(self):
        block = re.search(r"(?m)^permissions:\n((?:  .*\n)+)", CODE).group(1)
        self.assertEqual(dict(re.findall(r"^  ([a-z-]+):\s*(\S+)", block, re.M)),
                         {"contents": "read", "pull-requests": "write", "checks": "read", "statuses": "read", "actions": "read"})
        self.assertEqual(set(re.findall(r"secrets\.(\w+)", CODE)), {"GITHUB_TOKEN"})
        self.assertEqual(len(re.findall(r"(?m)^\s*permissions:", CODE)), 1)

    def test_no_polling_no_retry_and_no_way_to_start_claude_codex_or_another_workflow(self):
        for name, block in step_blocks().items():
            if "run:" in block:
                self.assertNotRegex(script_of(name), r"\b(sleep|while|until|retry|watch)\b|gh (workflow|run|pr)\b|@codex", name)
        self.assertNotRegex(CODE, r"workflow_dispatch:\n(?:.*\n)*?\s+(?:actions: write)")

    def test_the_real_handoff_is_still_locked_and_require_ci_is_still_on(self):
        cfg = json.loads((g.GATE / "config.json").read_text())
        self.assertIs(cfg["require_ci"], True)
        self.assertIs(cfg["real_handoff_enabled"], False)
        self.assertEqual(len(re.findall(r"vars\.BRIDGE_HANDOFF_MODE", CODE)), 3)
        self.assertIn("real_handoff_enabled", script_of("Handoff (REAL label, gated by BRIDGE_HANDOFF_MODE=label)").splitlines()[0])


if __name__ == "__main__":
    unittest.main()
