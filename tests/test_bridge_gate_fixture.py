"""The bridge-gate TEST FIXTURE dry run: bridge-gate-fixture.yml + bridge-gate/fixtures/needs-fix.facts.json.

Guarantees pinned here:
  * synthetic facts are REJECTED by gate.jq in every normal path and accepted only with the exact test-run token;
  * the fixture workflow can write nothing to GitHub: read-only token, no label/comment code, no vars, no secrets,
    and its scripts never call `gh` (executed below against a poisoned `gh` that records any call);
  * no production file (bridge-gate.yml, collect.sh, guard.sh, render.jq) references the fixture or the token;
  * the workflow demonstrates NEEDS_FIX attempt 1 of 3, one simulated handoff, and no duplicate handoff.
No GitHub, no Claude, no Codex.
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
from .gate_sim import check, facts, inline, review, run_gate, sha
from .test_bridge_gate_collect import Fixture

ROOT = g.ROOT
WF = ROOT / ".github" / "workflows" / "bridge-gate-fixture.yml"
TEXT = WF.read_text()
CODE = "\n".join(l for l in TEXT.splitlines() if not l.lstrip().startswith("#"))
FIXTURE = g.GATE / "fixtures" / "needs-fix.facts.json"
TOKEN = "test-fixture-run"
CONTROL, EVENT1, EVENT2, SUMMARY = (
    "Control (production handling must REJECT the fixture)",
    "Event 1 (fixture current-head finding) expects NEEDS_FIX, attempt 1 of 3",
    "Event 2 (the same event delivered again) expects no second handoff", "Summary")


def step_scripts():
    """step name -> run script, from the YAML text (no PyYAML)."""
    out, name, lines = {}, None, CODE.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^      - name: (.+)$", line)
        if m:
            name = m.group(1).strip()
        if name and re.match(r"^        run: \|$", line):
            body = []
            for l in lines[i + 1:]:
                if l.strip() and not l.startswith("          "):
                    break
                body.append(l[10:])
            out[name] = "\n".join(body) + "\n"
    return out


SCRIPTS = step_scripts()


def gate(facts_obj, *extra):
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(["jq", "-n", *extra, "--slurpfile", "facts", "/dev/stdin", "--slurpfile", "cfg",
                               str(g.GATE / "config.json"), "-f", str(g.GATE / "gate.jq")],
                              input=json.dumps(facts_obj), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def fixture_facts():
    return json.loads(FIXTURE.read_text())


class FixtureDataTests(unittest.TestCase):
    def test_the_fixture_is_explicitly_synthetic_and_cannot_be_mistaken_for_a_real_pr(self):
        f = fixture_facts()
        self.assertTrue(f["_fixture"].startswith("TEST FIXTURE"))
        self.assertIn("impersonates", f["_fixture"])
        self.assertEqual(f["pr"]["number"], 999999)
        self.assertNotEqual(f["pr"]["number"], 2)
        self.assertEqual(f["handoff_mode"], "simulate")
        self.assertIsNone(f["state"])
        self.assertEqual(f["incomplete"], [])

    def test_it_models_exactly_one_current_head_finding(self):
        f = fixture_facts()
        head = f["pr"]["head_sha"]
        self.assertEqual([(r["id"], r["commit_id"]) for r in f["reviews"]], [(1, head)])
        self.assertEqual(f["review_comments"], [{"review_id": 1, "in_reply_to": None}])
        self.assertEqual(f["check_runs"][0]["conclusion"], "success")

    def test_the_fixture_head_is_not_prs_real_head(self):
        self.assertNotEqual(fixture_facts()["pr"]["head_sha"], "abc17c8b4c56b28f4b541100b677032b2652d234")


class RejectionTests(unittest.TestCase):
    def test_normal_handling_rejects_the_fixture_and_hands_off_nothing(self):
        d = gate(fixture_facts())
        self.assertEqual((d["decision"], d["reason"], d["problems"], d["handoff"], d["state_changed"], d["notify"]),
                         ("BLOCKED", "EVIDENCE_UNAVAILABLE", ["fixture_rejected"], None, False, False))

    def test_only_the_exact_token_accepts_it(self):
        for wrong in ("", "yes", "true", "1", "test-fixture-run ", "TEST-FIXTURE-RUN", "test-fixture"):
            with self.subTest(wrong):
                self.assertEqual(gate(fixture_facts(), "--arg", "allow_fixture", wrong)["problems"], ["fixture_rejected"])
        self.assertEqual(gate(fixture_facts(), "--arg", "allow_fixture", TOKEN)["decision"], "NEEDS_FIX")

    def test_a_differently_named_argument_does_not_open_it(self):
        self.assertEqual(gate(fixture_facts(), "--arg", "fixture_ok", TOKEN)["problems"], ["fixture_rejected"])
        self.assertEqual(gate(fixture_facts(), "--arg", "allow-fixture", TOKEN)["problems"], ["fixture_rejected"])

    def test_any_facts_carrying_the_marker_are_rejected_even_if_otherwise_perfect(self):
        real_looking = facts(reviews=[review(101, sha(1))], review_comments=[inline(101)], check_runs=[check("t")])
        self.assertEqual(run_gate(real_looking)["decision"], "NEEDS_FIX")        # fine without the marker
        real_looking["_fixture"] = "x"
        d = run_gate(real_looking)
        self.assertEqual((d["decision"], d["problems"], d["handoff"]), ("BLOCKED", ["fixture_rejected"], None))

    def test_real_facts_are_not_flagged_as_fixtures(self):
        self.assertIsNone(run_gate(facts(reviews=[review(101, sha(1))], review_comments=[inline(101)]))["fixture"])

    def test_the_marker_can_never_come_from_the_collector(self):
        fx = Fixture()
        self.assertNotIn("_fixture", fx.collect())
        self.assertNotIn("_fixture", (g.GATE / "collect.sh").read_text())

    def test_no_production_file_references_the_fixture_or_the_token(self):
        production = [ROOT / ".github/workflows/bridge-gate.yml", g.GATE / "collect.sh", g.GATE / "guard.sh", g.GATE / "render.jq",
                      g.GATE / "config.json"]
        for path in production:
            text = path.read_text()
            for needle in ("allow_fixture", "test-fixture-run", "fixtures/", "needs-fix.facts"):
                self.assertNotIn(needle, text, (path.name, needle))

    def test_only_the_fixture_workflow_and_gate_jq_know_the_token_name(self):
        holders = sorted(p.relative_to(ROOT).as_posix() for p in list(ROOT.rglob("*")) if p.is_file() and ".git/" not in p.as_posix()
                         and p.suffix in (".yml", ".jq", ".sh", ".json") and "allow_fixture" in p.read_text())
        self.assertEqual(holders, [".github/workflows/bridge-gate-fixture.yml", "bridge-gate/gate.jq"])


class StaticWorkflowTests(unittest.TestCase):
    def test_triggers_are_exactly_the_test_branch_push_and_manual_dispatch(self):
        on_block = CODE.split("\non:", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual(re.findall(r"^  ([a-z_]+):", on_block, re.M), ["push", "workflow_dispatch"])
        self.assertIn("branches: [test/bridge-gate-fixture]", on_block)
        self.assertNotIn("tags", on_block)
        self.assertNotIn("paths", on_block)

    def test_the_token_is_read_only_and_nothing_can_write_to_github(self):
        block = re.search(r"(?m)^permissions:\n((?:  .*\n)+)", CODE).group(1)
        self.assertEqual(dict(re.findall(r"^  ([a-z-]+):\s*(\S+)", block, re.M)), {"contents": "read"})
        self.assertEqual(len(re.findall(r"(?m)^\s*permissions:", CODE)), 1)
        for forbidden in (r"\bgh\b", r"-X\b", "labels", r"issues/", r"pulls/", "curl", "wget", "api.github.com", "git push", "merge",
                          "approve", "deploy", "release", "GITHUB_TOKEN", r"secrets\.", r"vars\.", r"\benv:", "id-token"):
            self.assertNotRegex(CODE, forbidden, forbidden)

    def test_no_real_handoff_code_and_no_ai_references(self):
        self.assertNotIn("bridge:needs-fix", CODE)
        self.assertNotRegex(CODE, r"(?i)anthropic|claude|openai|chatgpt|codex|copilot")
        self.assertEqual(re.findall(r"(?m)^\s*-?\s*uses:\s*(\S+)", CODE), ["actions/checkout@v4"])
        self.assertIn("persist-credentials: false", CODE)

    def test_scripts_use_no_event_data_and_a_fixed_fixture_path(self):
        self.assertEqual(set(SCRIPTS), {CONTROL, EVENT1, EVENT2, SUMMARY})
        for name, script in SCRIPTS.items():
            self.assertNotIn("${{", script, name)
        for name in (CONTROL, EVENT1):
            self.assertIn("bridge-gate/fixtures/needs-fix.facts.json", SCRIPTS[name])

    def test_only_the_two_event_steps_pass_the_token_and_the_control_does_not(self):
        self.assertNotIn("allow_fixture", SCRIPTS[CONTROL])
        self.assertEqual(SCRIPTS[EVENT1].count("--arg allow_fixture test-fixture-run"), 1)
        self.assertEqual(SCRIPTS[EVENT2].count("--arg allow_fixture test-fixture-run"), 1)

    def test_short_job_confined_to_the_test_repository_and_banner_present(self):
        self.assertRegex(CODE, r"(?m)^    timeout-minutes: [1-5]$")
        self.assertIn("github.repository == 'fcapiral-bot/ai-automation-public-test'", CODE)
        self.assertEqual(len(re.findall(r"(?m)^    runs-on:", CODE)), 1)
        self.assertIn("TEST FIXTURE ONLY", TEXT)
        self.assertIn("TEST FIXTURE", CODE)
        self.assertRegex(CODE, r"concurrency:\n  group: bridge-gate-fixture\n  cancel-in-progress: false")

    def test_the_workflow_is_small(self):
        self.assertLess(len(TEXT.splitlines()), 80)


class ExecutedWorkflowTests(unittest.TestCase):
    """Runs the workflow's own scripts with bash exactly as Actions does, with `gh` poisoned."""

    def setUp(self):
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="gate-fixture-"))
        atexit.register(shutil.rmtree, self.work, True)
        (self.work / "bridge-gate").symlink_to(g.GATE)
        self.bin = self.work / "bin"; self.bin.mkdir()
        self.poison = self.work / "gh-was-called"
        gh = self.bin / "gh"
        gh.write_text('#!/usr/bin/env bash\necho "$*" >> "%s"\nexit 1\n' % self.poison); gh.chmod(0o755)
        self.summary = self.work / "summary.md"
        self.env = {"PATH": "%s:/usr/bin:/bin:/usr/local/bin" % self.bin, "HOME": str(self.work), "GITHUB_STEP_SUMMARY": str(self.summary)}

    def run_steps(self, names=(CONTROL, EVENT1, EVENT2, SUMMARY), stop_on_failure=True):
        out = {}
        for name in names:
            script = self.work / "step.sh"; script.write_text(SCRIPTS[name])
            proc = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", str(script)], cwd=self.work,
                                  env=self.env, capture_output=True, text=True)
            out[name] = proc
            if proc.returncode != 0 and stop_on_failure:
                break
        return out

    def test_the_run_shows_needs_fix_attempt_1_of_3_one_simulated_handoff_and_no_duplicate(self):
        out = self.run_steps()
        self.assertEqual([p.returncode for p in out.values()], [0, 0, 0, 0], {k: v.stderr for k, v in out.items()})
        self.assertIn("fixture_rejected", out[CONTROL].stdout)
        self.assertIn('"decision":"NEEDS_FIX"', out[EVENT1].stdout)
        self.assertEqual(out[EVENT1].stdout.count("SIMULATED HANDOFF"), 1)
        self.assertIn("attempt 1 of 3", out[EVENT1].stdout)
        self.assertIn("nothing started", out[EVENT1].stdout)
        self.assertIn('"status":"in_flight"', out[EVENT1].stdout)
        self.assertIn('"decision":"WAIT","reason":"HANDOFF_IN_FLIGHT","duplicate":true,"handoff":null', out[EVENT2].stdout)
        self.assertIn("handoffs issued in this run: 1 (expected 1)", out[EVENT2].stdout)
        self.assertNotIn("SIMULATED HANDOFF", out[EVENT2].stdout)
        text = self.summary.read_text()
        self.assertIn("TEST FIXTURE", text)
        self.assertIn("NEEDS_FIX / CODEX_FINDINGS, attempt 1 of 3, simulated handoff", text)

    def test_no_script_ever_calls_the_github_cli(self):
        self.run_steps()
        self.assertFalse(self.poison.exists(), self.poison.read_text() if self.poison.exists() else "")

    def test_the_run_writes_nothing_outside_its_working_directory_files(self):
        before = {p.name for p in self.work.iterdir()}
        self.run_steps()
        created = {p.name for p in self.work.iterdir()} - before
        self.assertEqual(created, {"rejected.json", "decision1.json", "decision2.json", "facts2.json", "step.sh", "summary.md"} - before)

    def test_a_doctored_fixture_without_the_marker_fails_the_control_and_the_job_stops(self):
        f = fixture_facts(); f.pop("_fixture")
        bad = self.work / "fixtures-copy"; shutil.copytree(g.GATE, bad, ignore=shutil.ignore_patterns("fixtures"))
        (bad / "fixtures").mkdir(); (bad / "fixtures" / "needs-fix.facts.json").write_text(json.dumps(f))
        (self.work / "bridge-gate").unlink(); (self.work / "bridge-gate").symlink_to(bad)
        out = self.run_steps()
        self.assertNotEqual(out[CONTROL].returncode, 0)
        self.assertEqual(list(out), [CONTROL])  # nothing after the failed control runs

    def test_a_gate_that_stopped_rejecting_would_fail_the_control(self):
        weak = self.work / "weak"; shutil.copytree(g.GATE, weak)
        jq = (weak / "gate.jq").read_text()
        self.assertIn('!= "test-fixture-run"', jq)
        (weak / "gate.jq").write_text(jq.replace('!= "test-fixture-run"', '!= "__never__"').replace("fixture_rejected", "x"))
        (self.work / "bridge-gate").unlink(); (self.work / "bridge-gate").symlink_to(weak)
        self.assertNotEqual(self.run_steps()[CONTROL].returncode, 0)


if __name__ == "__main__":
    unittest.main()
