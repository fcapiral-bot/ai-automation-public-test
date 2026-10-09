"""bridge-gate.yml: static guardrails, and the workflow's REAL shell steps executed against a fake `gh`.

Static tests pin triggers, permissions and the gating of the (inactive) real handoff. Execution tests
pull each step's `run:` script out of the YAML and run it with bash exactly as Actions would
(`bash --noprofile --norc -e -o pipefail`), stopping at the first failing step, so write-ahead
ordering and the simulated-versus-real handoff are tested on the actual glue, not on a copy of it.
No network, no GitHub, no AI.
"""

import atexit
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

from . import gate_sim as g
from .gate_sim import CODEX, check, facts, inline, review, sha, summary
from .test_bridge_gate_collect import BOT, FAKES, Fixture, state_comment_body, user

ROOT = g.ROOT
WORKFLOW = ROOT / ".github" / "workflows" / "bridge-gate.yml"
TEXT = WORKFLOW.read_text()
CODE = "\n".join(l for l in TEXT.splitlines() if not l.lstrip().startswith("#"))

COLLECT, DECIDE, GUARD, PERSIST, NOTIFY, SIMULATED, REAL, SUMMARY = (
    "Collect evidence (read-only)", "Decide", "Guard (head unchanged)", "Persist state", "Notify on READY, BLOCKED, MAX_ATTEMPTS or USAGE_STOP",
    "Handoff (SIMULATED, log only)", "Handoff (REAL label, gated by BRIDGE_HANDOFF_MODE=label)", "Summary")
PIPELINE = [COLLECT, DECIDE, GUARD, PERSIST, NOTIFY, SIMULATED, SUMMARY]


def step_blocks():
    """name -> raw text of each step in the single job."""
    blocks, cur, name = {}, [], None
    for line in CODE.splitlines():
        if re.match(r"^      - ", line):
            if name:
                blocks[name] = "\n".join(cur)
            cur, name = [line], None
            m = re.match(r"^      - (?:name|uses): (.+)$", line)
            name = m.group(1).strip() if m else None
        elif cur:
            cur.append(line)
            m = re.match(r"^        name: (.+)$", line)
            if m and name is None:
                name = m.group(1).strip()
    if name:
        blocks[name] = "\n".join(cur)
    return blocks


def script_of(name):
    lines = step_blocks()[name].splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^        run: \|$", line):
            body = []
            for l in lines[i + 1:]:
                if l.strip() and not l.startswith("          "):
                    break
                body.append(l[10:])
            return "\n".join(body) + "\n"
        m = re.match(r"^        run: (.+)$", line)
        if m:
            return m.group(1) + "\n"
    raise AssertionError("no run script in step " + name)


class StaticTests(unittest.TestCase):
    def test_triggers_are_exactly_the_event_set_and_no_scheduled_or_privileged_ones(self):
        on_block = CODE.split("\non:", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual(re.findall(r"^  ([a-z_]+):", on_block, re.M),
                         ["pull_request", "pull_request_review", "issue_comment", "workflow_run", "workflow_dispatch"])
        self.assertIn("types: [synchronize, reopened, ready_for_review]", on_block)
        # workflow_run is allowed for exactly one purpose and nothing else: waking the gate when `ci` completes.
        # tests/test_bridge_gate_ci_wakeup.py pins how it may be used.
        self.assertIn("workflow_run:\n    workflows: [ci]\n    types: [completed]", on_block)
        for forbidden in ("schedule", "push", "pull_request_target", "check_suite", "check_run", "status",
                          "repository_dispatch", "release", "issues", "create", "delete", "deployment", "workflow_call"):
            self.assertNotRegex(on_block, r"(?m)^\s*%s:" % forbidden, forbidden)

    def test_permissions_are_the_pinned_minimum(self):
        block = re.search(r"(?m)^permissions:\n((?:  .*\n)+)", CODE).group(1)
        self.assertEqual(dict(re.findall(r"^  ([a-z-]+):\s*(\S+)", block, re.M)),
                         {"contents": "read", "pull-requests": "write", "checks": "read", "statuses": "read", "actions": "read"})
        self.assertEqual(len(re.findall(r"(?m)^\s*permissions:", CODE)), 1)
        for bad in ("write-all", "id-token", "contents: write", "actions: write", "issues: write", "checks: write"):
            self.assertNotIn(bad, CODE)

    def test_no_secrets_but_the_automatic_token_and_only_github_owned_actions(self):
        self.assertEqual(set(re.findall(r"secrets\.(\w+)", CODE)), {"GITHUB_TOKEN"})
        self.assertEqual(re.findall(r"(?m)^\s*-?\s*uses:\s*(\S+)", CODE), ["actions/checkout@v4"])
        self.assertIn("persist-credentials: false", CODE)
        self.assertNotRegex(CODE, r"(?i)anthropic|claude|openai|chatgpt(?!-codex-connector)|copilot")

    def test_codex_is_named_only_as_the_reviewer_identity_filter(self):
        stripped = CODE.replace("chatgpt-codex-connector[bot]", "")
        self.assertNotRegex(stripped, r"(?i)codex")
        self.assertEqual(CODE.count("chatgpt-codex-connector[bot]"), 1)  # the issue_comment filter, nothing else

    def test_no_run_script_interpolates_event_data(self):
        for name, block in step_blocks().items():
            if "run:" in block:
                self.assertNotIn("${{", script_of(name), name)

    def test_no_command_that_merges_deploys_pushes_approves_or_calls_out(self):
        forbidden = r"\b(merge|deploy|publish|approve|curl|wget|npm|pip|docker|sudo)\b|git (push|commit|merge)|gh (pr|workflow|release|run)\b|auto-?merge"
        for name, block in step_blocks().items():
            if "run:" in block:
                self.assertNotRegex(script_of(name), forbidden, name)

    def test_every_write_is_a_comment_or_the_one_label(self):
        allowed = (r"^repos/\$REPO/issues/comments/\$id$", r"^repos/\$REPO/issues/\$PR_NUMBER/comments$",
                   r"^repos/\$REPO/issues/\$PR_NUMBER/labels$", r"^repos/\$REPO/issues/\$PR_NUMBER/labels/\$enc$")
        writes = re.findall(r'gh api -X (POST|PATCH|DELETE) "([^"]+)"', CODE)
        self.assertTrue(writes)
        for method, path in writes:
            self.assertTrue(any(re.match(a, path) for a in allowed), (method, path))
        self.assertNotRegex(CODE, r"gh api(?! -X)[^\n]*(-f|-F) ")  # no implicit POSTs

    def test_the_real_handoff_is_the_only_label_writer_and_is_gated_off_by_default(self):
        label_steps = [n for n, b in step_blocks().items() if "/labels" in b]
        self.assertEqual(label_steps, [REAL])
        self.assertIn("if: ${{ vars.BRIDGE_HANDOFF_MODE == 'label' }}", step_blocks()[REAL])
        self.assertIn("if: ${{ vars.BRIDGE_HANDOFF_MODE != 'label' }}", step_blocks()[SIMULATED])
        # the variable is read in exactly three places: the job env (passed to the collector) and the two step gates
        self.assertEqual(len(re.findall(r"vars\.BRIDGE_HANDOFF_MODE", CODE)), 3)

    def test_steps_run_in_write_ahead_order(self):
        order = [n for n in step_blocks() if n != "actions/checkout@v4"]
        self.assertEqual(order, [COLLECT, DECIDE, GUARD, PERSIST, NOTIFY, SIMULATED, REAL, SUMMARY])

    def test_runs_are_serialised_per_pr_and_never_cancelled(self):
        self.assertRegex(CODE, r"concurrency:\n  group: bridge-gate-\$\{\{ github\.event\.pull_request\.number \|\| github\.event\.issue\.number \|\| inputs\.pr_number"
                         r" \|\| github\.event\.workflow_run\.pull_requests\[0\]\.number \|\| github\.run_id \}\}\n  cancel-in-progress: false")

    def test_job_is_short_and_confined_to_the_test_repository(self):
        self.assertRegex(CODE, r"(?m)^    timeout-minutes: [1-5]$")
        self.assertIn("github.repository == 'fcapiral-bot/ai-automation-public-test'", CODE)
        self.assertEqual(len(re.findall(r"(?m)^    runs-on:", CODE)), 1)

    def test_issue_comments_are_filtered_to_codex_on_pull_requests(self):
        self.assertIn("github.event.issue.pull_request && github.event.comment.user.login == 'chatgpt-codex-connector[bot]'", CODE)

    def test_the_gate_is_small(self):
        self.assertLess(len(TEXT.splitlines()), 150)
        self.assertLess(len((g.GATE / "gate.jq").read_text().splitlines()), 150)
        self.assertEqual(sorted(p.name for p in g.GATE.glob("*.sh")), ["collect.sh", "guard.sh"])

    def test_config_defaults_are_the_documented_safe_ones(self):
        c = g.CONFIG
        self.assertEqual((c["max_attempts"], c["require_ci"], c["codex"]["id"], c["notify_mention"]), (3, True, 199175422, ""))
        self.assertIs(c["real_handoff_enabled"], False)  # the committed lock on the real label handoff


class Runner:
    """Executes the workflow's steps in order against a Fixture, like Actions (stop at first failure)."""

    def __init__(self, fx, unlock=False, **env):
        self.fx = fx
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="gate-work-"))
        atexit.register(shutil.rmtree, self.work, True)
        (self.work / "bridge-gate").symlink_to(g.GATE)
        # The real step reads the lock from the DEFAULT BRANCH over the API (never from the checkout).
        fx.put("contents_bridge-gate_config.json", {**g.CONFIG, "real_handoff_enabled": bool(unlock)})
        self.summary = self.work / "summary.md"
        self.base = fx.env(GITHUB_RUN_ID="99", GITHUB_STEP_SUMMARY=str(self.summary), GH_TOKEN="fake", REVIEW_ID="101",
                           HEAD_SHA=HEAD_DEFAULT, COMMENT_ID="", COMMENT_UPDATED="", DEFAULT_BRANCH="main")
        self.base.update(env)
        self.out = {}

    def run(self, steps=PIPELINE, **env):
        self.out = {}
        for name in steps:
            script = self.work / "step.sh"
            script.write_text(script_of(name))
            proc = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", str(script)], cwd=self.work,
                                  env={**self.base, **env}, capture_output=True, text=True)
            self.out[name] = proc
            if proc.returncode != 0:
                break
        return self.out

    def decision(self):
        return json.loads((self.work / "decision.json").read_text())

    def bodies(self):
        return sorted(self.fx.dir.glob("body-*.txt"))


HEAD_DEFAULT = sha(1)


def findings_fixture():
    fx = Fixture(HEAD_DEFAULT)
    fx.put("pulls_7_reviews", [{"id": 101, "user": user(CODEX), "state": "COMMENTED", "commit_id": HEAD_DEFAULT}])
    fx.put("pulls_7_comments", [{"pull_request_review_id": 101, "in_reply_to_id": None}])
    fx.put("commits_SHA_check-runs", {"total_count": 1, "check_runs": [
        {"name": "unit-tests", "status": "completed", "conclusion": "success", "check_suite": {"id": 100}}]})
    return fx


class ExecutedGlueTests(unittest.TestCase):
    def test_findings_persist_state_first_then_log_a_simulated_handoff_and_nothing_else(self):
        fx = findings_fixture()
        r = Runner(fx)
        out = r.run()
        self.assertEqual([p.returncode for p in out.values()], [0] * len(PIPELINE), {k: v.stderr for k, v in out.items()})
        self.assertEqual(r.decision()["decision"], "NEEDS_FIX")
        # exactly one write: the state comment (POST, no state comment existed yet)
        self.assertEqual(len(fx.writes()), 1)
        self.assertTrue(fx.writes()[0].startswith("POST repos/o/r/issues/7/comments"), fx.writes())
        first_line = r.bodies()[0].read_text().splitlines()[0]
        state = json.loads(first_line[len("<!-- bridge-gate-state:v1 "):-len(" -->")])
        self.assertEqual((state["attempts"][0]["status"], state["attempts"][0]["head"], state["last"]["decision"]),
                         ("in_flight", HEAD_DEFAULT, "NEEDS_FIX"))
        self.assertIn("SIMULATED HANDOFF", out[SIMULATED].stdout)
        self.assertIn("attempt 1/3", out[SIMULATED].stdout)
        self.assertIn("Nothing was started", out[SIMULATED].stdout)
        self.assertFalse([w for w in fx.writes() if "labels" in w])
        self.assertIn("Decision: `NEEDS_FIX`", r.summary.read_text())

    def test_the_same_event_again_is_a_duplicate_and_hands_off_nothing(self):
        fx = findings_fixture()
        r = Runner(fx)
        r.run()
        body = r.bodies()[0].read_text()
        fx.put("issues_7_comments", [{"id": 4242, "user": BOT, "body": body}])
        out = r.run()
        self.assertEqual(r.decision()["reason"], "HANDOFF_IN_FLIGHT")
        self.assertTrue(r.decision()["duplicate"])
        self.assertNotIn("SIMULATED HANDOFF", out[SIMULATED].stdout)
        # the state comment is edited in place (PATCH), never duplicated
        self.assertEqual([w.split()[0] for w in fx.writes()], ["POST", "PATCH"])
        self.assertIn("PATCH repos/o/r/issues/comments/4242", fx.writes()[1])
        # a third identical delivery changes nothing at all
        body2 = r.bodies()[-1].read_text()
        fx.put("issues_7_comments", [{"id": 4242, "user": BOT, "body": body2}])
        n = len(fx.writes())
        r.run()
        self.assertEqual(len(fx.writes()), n)

    def test_if_the_state_cannot_be_written_no_handoff_step_runs(self):
        fx = findings_fixture()
        (fx.dir / "fail_writes").write_text("1")
        r = Runner(fx)
        out = r.run()
        self.assertNotEqual(out[PERSIST].returncode, 0)
        self.assertEqual(list(out), [COLLECT, DECIDE, GUARD, PERSIST])  # stopped: notify/handoff/summary never ran
        self.assertEqual(fx.writes(), [])

    def test_an_api_failure_ends_in_a_block_with_no_writes_and_no_handoff(self):
        fx = findings_fixture()
        fx.fail("pulls_7_reviews")
        r = Runner(fx)
        out = r.run()
        self.assertEqual([p.returncode for p in out.values()], [0] * len(PIPELINE))
        self.assertEqual((r.decision()["decision"], r.decision()["reason"]), ("BLOCKED", "EVIDENCE_UNAVAILABLE"))
        self.assertEqual(fx.writes(), [])
        self.assertNotIn("SIMULATED HANDOFF", out[SIMULATED].stdout)

    def test_ready_posts_one_notification_and_never_anything_that_merges(self):
        fx = Fixture(HEAD_DEFAULT)
        fx.put("issues_7_comments", [{"id": 9000, "user": user(CODEX), "body": summary(HEAD_DEFAULT)["body"]}])
        fx.put("commits_SHA_check-runs", {"total_count": 1, "check_runs": [
            {"name": "unit-tests", "status": "completed", "conclusion": "success", "check_suite": {"id": 100}}]})
        r = Runner(fx)
        out = r.run()
        self.assertEqual(r.decision()["decision"], "READY")
        self.assertEqual([w.split()[0] for w in fx.writes()], ["POST", "POST"])  # state, notification
        note = r.bodies()[1].read_text()
        self.assertIn("READY for human review", note)
        self.assertIn("the gate never merges", note)
        self.assertNotIn("SIMULATED HANDOFF", out[SIMULATED].stdout)

    def test_kill_switch_variable_stops_everything(self):
        fx = findings_fixture()
        r = Runner(fx, KILL_SWITCH="true")
        r.run()
        self.assertEqual((r.decision()["decision"], r.decision()["handoff"]), ("USAGE_STOP", None))
        self.assertIn("USAGE_STOP", r.bodies()[1].read_text())

    def test_mention_is_configurable_and_off_by_default(self):
        cfg = json.loads((g.GATE / "config.json").read_text())
        cfg["notify_mention"] = "some-user"
        tmpdir = pathlib.Path(tempfile.mkdtemp()); atexit.register(shutil.rmtree, tmpdir, True)
        cfgfile = tmpdir / "c.json"; cfgfile.write_text(json.dumps(cfg))
        rendered = subprocess.run(["jq", "-r", "--arg", "kind", "notify", "--slurpfile", "cfg", str(cfgfile), "-f", str(g.GATE / "render.jq")],
                                  input=json.dumps({"decision": "READY", "reason": "x", "head": sha(1), "state": {"attempts": []}}),
                                  capture_output=True, text=True).stdout
        self.assertTrue(rendered.startswith("@some-user READY"), rendered)

    def test_the_real_handoff_script_adds_the_label_and_clears_it_when_superseded(self):
        fx = findings_fixture()
        r = Runner(fx, unlock=True, HANDOFF_MODE="label")
        r.run(steps=[COLLECT, DECIDE])
        fx_writes_before = len(fx.writes())
        out = r.run(steps=[REAL], HANDOFF_MODE="label")  # reuse decision.json from the pipeline above
        self.assertEqual(out[REAL].returncode, 0, out[REAL].stderr)
        added = fx.writes()[fx_writes_before:]
        self.assertEqual(added, ["POST repos/o/r/issues/7/labels labels[]=bridge:needs-fix"])
        # supersede: decision with clear_label and no handoff removes the (URL-encoded) label first
        decision = r.decision(); decision["clear_label"] = True; decision["handoff"] = None
        (r.work / "decision.json").write_text(json.dumps(decision))
        n = len(fx.writes())
        r.run(steps=[REAL], HANDOFF_MODE="label")
        self.assertEqual(fx.writes()[n:], ["DELETE repos/o/r/issues/7/labels/bridge%3Aneeds-fix"])

    def test_the_guard_runs_before_any_write_and_again_before_the_real_label(self):
        self.assertIn("bridge-gate/guard.sh decision.json", script_of(GUARD))
        lines = script_of(REAL).strip().splitlines()
        self.assertIn("real_handoff_enabled", lines[0])           # the committed lock comes first
        self.assertEqual(lines[1], "bridge-gate/guard.sh decision.json")

    def move_head(self, fx, n=2):
        pr = json.loads((fx.dir / "pulls_7.json").read_text()); pr["head"]["sha"] = sha(n); fx.put("pulls_7", pr)

    def test_stale_head_before_persist_writes_nothing_and_stops_the_pipeline(self):
        # Security review finding A3: the head moved after the decision. Nothing may be written or handed off.
        fx = findings_fixture()
        r = Runner(fx)
        r.run(steps=[COLLECT, DECIDE])
        self.move_head(fx)
        out = r.run(steps=[GUARD, PERSIST, NOTIFY, SIMULATED])
        self.assertNotEqual(out[GUARD].returncode, 0)
        self.assertIn("refusing a stale handoff", out[GUARD].stderr)
        self.assertEqual(list(out), [GUARD])
        self.assertEqual(fx.writes(), [])

    def test_stale_head_at_the_real_label_step_issues_no_label(self):
        fx = findings_fixture()
        r = Runner(fx, unlock=True, HANDOFF_MODE="label")
        r.run(steps=[COLLECT, DECIDE])
        self.move_head(fx)
        out = r.run(steps=[REAL], HANDOFF_MODE="label")
        self.assertNotEqual(out[REAL].returncode, 0)
        self.assertEqual([w for w in fx.writes() if "labels" in w], [])

    def test_an_unreadable_head_refuses_too(self):
        fx = findings_fixture()
        r = Runner(fx, HANDOFF_MODE="label")
        r.run(steps=[COLLECT, DECIDE])
        fx.fail("pulls_7")
        out = r.run(steps=[GUARD])
        self.assertNotEqual(out[GUARD].returncode, 0)
        self.assertIn("cannot re-read", out[GUARD].stderr)

    def test_an_unchanged_head_passes_and_a_decision_without_handoff_never_needs_the_api(self):
        fx = findings_fixture()
        r = Runner(fx)
        r.run(steps=[COLLECT, DECIDE])
        self.assertEqual(r.run(steps=[GUARD])[GUARD].returncode, 0)
        decision = r.decision(); decision["handoff"] = None
        (r.work / "decision.json").write_text(json.dumps(decision))
        fx.fail("pulls_7")
        self.assertEqual(r.run(steps=[GUARD])[GUARD].returncode, 0)

    def test_the_committed_lock_keeps_the_real_step_off_even_with_the_variable_set_to_label(self):
        # Security preflight: the repository variable alone must never enable the real handoff.
        fx = findings_fixture()
        r = Runner(fx, HANDOFF_MODE="label")  # lock closed (as committed)
        r.run(steps=[COLLECT, DECIDE])
        self.assertEqual(r.decision()["decision"], "NEEDS_FIX")  # a handoff IS due, so only the lock stops it
        out = r.run(steps=[REAL], HANDOFF_MODE="label")
        self.assertEqual(out[REAL].returncode, 0)
        self.assertIn("locked off", out[REAL].stdout)
        self.assertEqual(fx.writes(), [])
        decision = r.decision(); decision["clear_label"] = True
        (r.work / "decision.json").write_text(json.dumps(decision))
        r.run(steps=[REAL], HANDOFF_MODE="label")
        self.assertEqual(fx.writes(), [])  # not even the label removal

    def test_the_real_step_is_inert_without_a_handoff(self):
        fx = Fixture(HEAD_DEFAULT)
        r = Runner(fx, unlock=True, HANDOFF_MODE="label")
        r.run(steps=[COLLECT, DECIDE])
        r.run(steps=[REAL], HANDOFF_MODE="label")
        self.assertEqual(fx.writes(), [])


if __name__ == "__main__":
    unittest.main()
