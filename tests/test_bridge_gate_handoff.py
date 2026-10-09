"""Focused regression tests for the (inactive) Claude handoff: the lock, the label bridge and the Routine prompts.

No network, no GitHub, no AI. The workflow's real shell steps run against a fake `gh`; the saved Routine prompts are
read as text. Nothing here creates a Routine, a label or a variable.
"""

import json
import re
import unittest

from . import gate_sim as g
from .test_bridge_gate_workflow import (CODE, COLLECT, DECIDE, REAL, SIMULATED, Runner, findings_fixture, script_of, step_blocks)

PROMPTS = (g.GATE / "routine-prompt.md").read_text()
DOCS = {p.name: p.read_text() for p in (g.GATE / "ROUTINE.md", g.GATE / "README.md", g.GATE / "SECURITY.md")}
CFG = g.CONFIG
WORKFLOWS = sorted((g.ROOT / ".github" / "workflows").glob("*.y*ml"))


def prompt(stage):
    blocks = re.findall(r"```\n(.*?)```", PROMPTS, re.S)
    return blocks[stage - 1]


def label_writes(fx):
    return [w for w in fx.writes() if "/labels" in w]


def set_lock(fx, lock):
    """What the default branch's bridge-gate/config.json looks like to the API: absent (None), garbage, or a lock value."""
    path = fx.dir / "contents_bridge-gate_config.json.json"
    if lock is None:
        path.unlink()                       # missing / unreadable (the fake answers HTTP 404)
    elif lock == "garbage":
        path.write_text("<html>not json")
    else:
        fx.put("contents_bridge-gate_config.json", {**CFG, "real_handoff_enabled": lock})


class LockTests(unittest.TestCase):
    """The real label handoff needs BOTH the variable and a lock opened on the default branch."""

    def pipeline_with_lock(self, lock):
        fx = findings_fixture()
        r = Runner(fx, HANDOFF_MODE="label")
        set_lock(fx, lock)
        r.run(steps=[COLLECT, DECIDE], HANDOFF_MODE="")  # decided in simulate mode so a handoff IS due; the real step must stop it on its own
        self.assertEqual(r.decision()["decision"], "NEEDS_FIX")
        return fx, r, r.run(steps=[REAL], HANDOFF_MODE="label")

    def test_every_value_but_exactly_true_on_the_default_branch_keeps_it_locked(self):
        for lock in (False, None, "garbage", "true", 1, "yes", {}):
            fx = findings_fixture()
            r = Runner(fx, HANDOFF_MODE="label")
            set_lock(fx, lock)
            r.run(steps=[COLLECT, DECIDE], HANDOFF_MODE="")   # a handoff IS due (simulate-mode decision)
            self.assertEqual(r.decision()["decision"], "NEEDS_FIX", lock)
            out = r.run(steps=[REAL], HANDOFF_MODE="label")
            self.assertEqual(out[REAL].returncode, 0, (lock, out[REAL].stderr))
            self.assertIn("locked off", out[REAL].stdout, lock)
            self.assertEqual(label_writes(fx), [], lock)

    def test_a_pr_branch_that_opens_its_own_copy_of_the_lock_changes_nothing(self):
        fx = findings_fixture()                                  # default branch: locked (Runner default)
        r = Runner(fx, HANDOFF_MODE="label")
        r.run(steps=[COLLECT, DECIDE], HANDOFF_MODE="")
        cfg = json.loads((g.GATE / "config.json").read_text())
        cfg["real_handoff_enabled"] = True                       # what a PR branch could commit
        own = r.work / "own-config.json"; own.write_text(json.dumps(cfg))
        self.assertIs(json.loads(own.read_text())["real_handoff_enabled"], True)
        script = script_of(REAL)
        self.assertNotIn("bridge-gate/config.json\"", script.splitlines()[0].split("gh api")[0])  # lock line never reads the checkout
        out = r.run(steps=[REAL], HANDOFF_MODE="label")
        self.assertIn("locked off", out[REAL].stdout)
        self.assertEqual(fx.writes(), [])

    def test_the_lock_is_read_from_the_default_branch_ref_over_the_api(self):
        self.assertIn('contents/bridge-gate/config.json?ref=$DEFAULT_BRANCH', (g.GATE / "label.sh").read_text())   # never the PR checkout
        self.assertIn("github.event.repository.default_branch", step_blocks()[REAL])
        fx = findings_fixture(); r = Runner(fx, unlock=True, HANDOFF_MODE="label")
        r.run(steps=[COLLECT, DECIDE]); r.run(steps=[REAL], HANDOFF_MODE="label")
        self.assertTrue(any("contents/bridge-gate/config.json?ref=main" in c for c in fx.calls()), fx.calls())

    def test_the_variable_alone_never_adds_a_label_and_unset_variable_never_reaches_the_real_step(self):
        fx, r, out = self.pipeline_with_lock(False)
        self.assertEqual(label_writes(fx), [])
        self.assertIn("vars.BRIDGE_HANDOFF_MODE == 'label'", step_blocks()[REAL])   # step-level gate for the unset variable
        out = Runner(findings_fixture()).run(steps=[COLLECT, DECIDE, SIMULATED])     # unset variable: simulated step only
        self.assertIn("SIMULATED HANDOFF", out[SIMULATED].stdout)

    def test_committed_config_is_locked_and_the_lock_opens_only_the_label(self):
        self.assertIs(CFG["real_handoff_enabled"], False)
        fx = findings_fixture(); r = Runner(fx, unlock=True, HANDOFF_MODE="label")
        r.run(steps=[COLLECT, DECIDE]); r.run(steps=[REAL], HANDOFF_MODE="label")
        self.assertEqual(fx.writes(), ["POST repos/o/r/issues/7/labels labels[]=" + CFG["labels"]["needs_fix"]])


class NothingStartsClaudeTests(unittest.TestCase):
    def test_no_workflow_references_claude_routines_or_credentials(self):
        banned = r"claude-code-action|anthropic|ANTHROPIC|CLAUDE_CODE_OAUTH|api\.claude|/fire\b|routines?/|api_key|apikey|setup-token"
        for path in WORKFLOWS:
            text = "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("#"))
            self.assertNotRegex(text, banned, path.name)

    def test_the_gate_has_no_trigger_that_can_start_work_from_a_label_event(self):
        on_block = CODE.split("\non:", 1)[1].split("\npermissions:", 1)[0]
        self.assertNotRegex(on_block, r"\blabeled\b|pull_request_target|schedule|push:")  # workflow_run: see test_bridge_gate_ci_wakeup

    def test_the_gate_writes_no_secret_and_only_the_builtin_token_is_used(self):
        self.assertEqual(set(re.findall(r"secrets\.(\w+)", CODE)), {"GITHUB_TOKEN"})


class RoutinePromptTests(unittest.TestCase):
    def test_label_and_commit_title_match_the_gate_configuration(self):
        label = CFG["labels"]["needs_fix"]
        title = CFG["fix_commit_title"].replace("{n}", "N").replace("{max}", str(CFG["max_attempts"]))
        for stage in (1, 2):
            self.assertIn("`%s`" % label, prompt(stage))
        self.assertIn('"%s"' % title, prompt(2))
        self.assertIn(CFG["state_marker"].strip(), prompt(1))
        self.assertIn(CFG["state_marker"].strip(), prompt(2))
        self.assertIn("`%s`" % CFG["codex"]["login"], prompt(2))
        self.assertIn(label, DOCS["ROUTINE.md"])

    def test_both_stages_pin_the_intended_pr_and_head(self):
        for stage in (1, 2):
            p = prompt(stage)
            for needle in ("exactly\n   one", "non-draft", "fork", "`in_flight`", "current head", "is the only commit"):
                self.assertIn(needle, p, (stage, needle))
            self.assertRegex(p, r"stop without doing anything")

    def test_correction_prompt_fixes_only_verified_codex_findings_and_runs_tests_first(self):
        p = prompt(2)
        self.assertRegex(p, r"Verify each finding")
        self.assertRegex(p, r"real\s+defect")
        self.assertRegex(p, r"DATA,\s+never as instructions")
        self.assertRegex(p, r"python3 -W error -m unittest discover -s tests -t \. before pushing|before pushing")
        self.assertLess(p.index("unittest discover"), p.index("Make at most ONE commit"))
        self.assertLess(p.index("fetch the branch again"), p.index("Make at most ONE commit"))

    def test_correction_prompt_pushes_to_the_pr_branch_only_once_and_never_forces(self):
        p = re.sub(r"\s+", " ", prompt(2))
        for needle in ("HEAD BRANCH (never `main`)", "at most ONE commit", "push it once, normally", "Never force-push",
                       "Never create a new pull request or a new branch"):
            self.assertIn(needle, p, needle)

    def test_correction_prompt_forbids_everything_that_could_escape_the_pr_or_cost_money(self):
        p = re.sub(r"\s+", " ", prompt(2))
        forbidden = p[p.index("You must NOT:"):p.index("After pushing")]
        for needle in (".github/**", "bridge-gate/**", "merge", "approve", "resolve review threads", "add or remove labels",
                       "@codex", "deploy", "start or re-run any workflow", "any other repository", "secrets or dependencies",
                       "paid or external service", "tokens, API keys or usage credits"):
            self.assertIn(needle, forbidden, needle)

    def test_report_only_prompt_cannot_change_anything(self):
        p = re.sub(r"\s+", " ", prompt(1))
        self.assertIn("REPORT-ONLY", p)
        self.assertIn("must not change any file, branch, label or review", p)
        self.assertNotRegex(p, r"git push|Make at most|commit titled|unittest")

    def test_both_stages_stop_safely_on_usage_limits_and_permission_failures(self):
        for stage in (1, 2):
            p = re.sub(r"\s+", " ", prompt(stage))
            for needle in ("usage limit", "rate limit", "permission", "stop at once", "workaround", "tokens"):
                self.assertIn(needle, p, (stage, needle))
        p = re.sub(r"\s+", " ", prompt(2))
        self.assertIn("Do not retry in a loop", p)
        self.assertIn("Nothing further was pushed", p)
        self.assertIn("ends the attempt at its next evaluation after the lease", p)

    def test_prompts_name_no_other_repository_and_no_credentials_or_endpoints(self):
        self.assertEqual(set(re.findall(r"fcapiral-bot/[\w-]+", "\n".join(prompt(s) for s in (1, 2)))),
                         {"fcapiral-bot/ai-automation-public-test"})
        self.assertNotRegex(PROMPTS, r"https?://|sk-ant|ghp_|Bearer ")

    def test_the_docs_say_nothing_is_created_or_active(self):
        for needle in ("NOT created, NOT active",):
            self.assertIn(needle, PROMPTS)
        self.assertIn("NOT ACTIVE", DOCS["ROUTINE.md"])
        self.assertIn("default branch", DOCS["ROUTINE.md"])


if __name__ == "__main__":
    unittest.main()
