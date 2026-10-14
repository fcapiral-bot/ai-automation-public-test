"""The disposable end-to-end test: the synthetic bug is real and small, the branch script is safe, the runbook says what it must.

No network, no GitHub, no AI. Runs the fixture and the script in temporary directories; creates no Routine, label or variable.
"""

import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

from . import gate_sim as g

E2E = g.GATE / "e2e"
RUNBOOK = re.sub(r"\s+", " ", (g.GATE / "E2E-TEST.md").read_text())
SCRIPT = E2E / "make-synthetic-branch.sh"


def unit_tests(root):
    return subprocess.run(["python3", "-W", "error", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                          cwd=root, capture_output=True, text=True)


class SyntheticBugTests(unittest.TestCase):
    def tree(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="e2e-fixture-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "synthetic").mkdir(); (root / "tests").mkdir()
        (root / "synthetic" / "__init__.py").touch(); (root / "tests" / "__init__.py").touch()
        shutil.copy(E2E / "synthetic" / "pricing.py.txt", root / "synthetic" / "pricing.py")
        shutil.copy(E2E / "synthetic" / "test_pricing.py.txt", root / "tests" / "test_synthetic_pricing.py")
        return root

    def test_the_bug_fails_ci_and_the_one_line_fix_passes_it(self):
        root = self.tree()
        bad = unit_tests(root)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("FAILED (failures=4)", bad.stderr)
        src = root / "synthetic" / "pricing.py"
        text = src.read_text()
        fixed = text.replace("return price_cents * percent // 100", "return price_cents - price_cents * percent // 100")
        self.assertNotEqual(text, fixed)
        src.write_text(fixed)
        self.assertEqual(unit_tests(root).returncode, 0)
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("    return")]), 1)

    def test_the_fixture_is_inert_in_this_repository(self):
        """Not importable and not discovered here: only the throwaway branch gets real .py files."""
        self.assertFalse((g.ROOT / "synthetic").exists())
        self.assertEqual(list(E2E.rglob("*.py")), [])
        self.assertEqual(list((g.ROOT / "tests").glob("test_synthetic*")), [])


class BranchScriptTests(unittest.TestCase):
    def repo(self):
        base = pathlib.Path(tempfile.mkdtemp(prefix="e2e-branch-"))
        self.addCleanup(shutil.rmtree, base, True)
        run = lambda *a, cwd=base: subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True)
        run("git", "init", "-q", "--bare", "-b", "main", "origin.git")
        run("git", "clone", "-q", "origin.git", "work")
        work = base / "work"
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            run("git", "config", k, v, cwd=work)
        (work / "tests").mkdir(); (work / "tests" / "__init__.py").touch()
        shutil.copytree(E2E, work / "bridge-gate" / "e2e")
        run("git", "add", "-A", cwd=work); run("git", "commit", "-q", "-m", "base", cwd=work); run("git", "push", "-q", "origin", "HEAD:main", cwd=work)
        return work, run

    def test_it_makes_one_local_commit_on_a_claude_branch_and_pushes_nothing(self):
        work, run = self.repo()
        out = run("bridge-gate/e2e/make-synthetic-branch.sh", "a", cwd=work)
        self.assertEqual(run("git", "branch", "--show-current", cwd=work).stdout.strip(), "claude/e2e-synthetic-a")
        self.assertEqual(run("git", "rev-list", "--count", "origin/main..HEAD", cwd=work).stdout.strip(), "1")
        self.assertEqual(run("git", "ls-remote", "--heads", "origin", cwd=work).stdout.count("refs/heads/"), 1)   # only main
        self.assertIn("NON-DRAFT", out.stdout)
        self.assertEqual(unit_tests(work).returncode, 1)          # the branch carries the failing check on purpose

    def test_it_refuses_a_bad_suffix_and_a_dirty_tree(self):
        work, run = self.repo()
        for bad in ("A b", "../x", "x" * 21, ""):
            self.assertNotEqual(subprocess.run([str(work / SCRIPT.relative_to(g.ROOT)), bad], cwd=work, capture_output=True).returncode, 0, bad)
        (work / "stray.txt").write_text("x")
        self.assertNotEqual(subprocess.run([str(work / SCRIPT.relative_to(g.ROOT)), "a"], cwd=work, capture_output=True).returncode, 0)

    def test_it_never_pushes_opens_or_labels(self):
        code = re.sub(r"(?m)^\s*#.*|echo .*", "", SCRIPT.read_text())
        self.assertNotRegex(code, r"git push|gh |curl|label|merge|api")


class RunbookTests(unittest.TestCase):
    def test_the_runbook_keeps_every_safety_rule(self):
        for needle in ("NOT RUN", "claude/e2e-synthetic-*", "Never label PR #2", "Never merge a test PR", "BRIDGE_KILL_SWITCH=true",
                       "Credits must be $0", "**non-draft**", "unmerged", "Do not create a token or an API trigger",
                       "S10 and S18", "Stage 1 report-only", "Stage 2 correction", "Address review findings (bridge attempt 2/3)",
                       "bridge-watchdog", "MAX_ATTEMPTS", "HANDOFF_TIMEOUT", "READY", "real_handoff_enabled"):
            self.assertIn(needle, RUNBOOK, needle)

    def test_the_steps_that_the_security_notes_point_to_exist(self):
        sec = (g.GATE / "SECURITY.md").read_text()
        self.assertIn("`E2E-TEST.md` step 9", sec)
        self.assertIn("`E2E-TEST.md` steps 5 and 8", sec)
        self.assertRegex(RUNBOOK, r"## 5\. Trigger and PR identity.*S17")
        self.assertRegex(RUNBOOK, r"## 8\. Does a bot-applied label fire the Routine\? \(checks `SECURITY.md` S17")
        self.assertRegex(RUNBOOK, r"## 9\. The watchdog \(checks `SECURITY.md` S15\)")

    def test_the_stage_one_prompt_comes_before_the_correction_prompt_and_the_locks_open_late(self):
        self.assertLess(RUNBOOK.index("Stage 1 report-only"), RUNBOOK.index("## 7. Open both locks"))
        self.assertLess(RUNBOOK.index("## 7. Open both locks"), RUNBOOK.index("## 10. The correction loop"))
        self.assertLess(RUNBOOK.index("## 10. The correction loop"), RUNBOOK.index("**Stage 2 correction** prompt"))
        self.assertIn("`real_handoff_enabled` stays `false`", RUNBOOK)

    def test_the_runbook_records_the_observed_simulation_history_and_keeps_every_counter(self):
        for needle in ("Observed 2026-10-14", "PR #4", "PR #5", "`7c1446a`", "`6663826`", "BLOCKED / HANDOFF_TIMEOUT", "`timed_out`",
                       "simulation history and still count", "2 of 3 attempts left", "same head can never get another attempt",
                       "nothing reset"):
            self.assertIn(needle, RUNBOOK, needle)
        self.assertNotRegex(RUNBOOK, r"On PR A delete the bot's state comment|resets its attempt count on purpose")
        self.assertRegex(RUNBOOK, r"never reset silently")
        self.assertRegex(RUNBOOK, r"it is \*\*not\*\* part of this test")

    def test_a_real_handoff_test_starts_on_a_fresh_pr_with_the_locks_already_open(self):
        step7 = RUNBOOK[RUNBOOK.index("## 7. Open both locks"):RUNBOOK.index("## 8.")]
        for needle in ("Close A and B **unmerged** first", "fresh PR C", "make-synthetic-branch.sh c", "**non-draft**",
                       "do not delete any state comment", "BLOCKED / HANDOFF_LOCKED"):
            self.assertIn(needle, step7, needle)
        self.assertLess(step7.index("`BRIDGE_HANDOFF_MODE` ="), step7.index("**Then** create a fresh PR C"))

    def test_stage_one_no_longer_expects_a_session_on_an_in_flight_attempt_to_stop(self):
        step5 = RUNBOOK[RUNBOOK.index("## 5. Trigger"):RUNBOOK.index("## 6.")]
        self.assertNotIn("No attempt is in flight (the gate recorded none)", RUNBOOK)
        for needle in ("no attempt `in_flight`", "`timed_out`", "A (PR #4) only", "B (PR #5)", "stop at step 2", "an attempt was `in_flight` when you labelled"):
            self.assertIn(needle, step5, needle)
        self.assertIn("Read the state comment before every manual label", RUNBOOK)
        self.assertIn("Add a label by hand only if no attempt is `in_flight`", RUNBOOK)

    def test_expired_leases_are_explained_so_they_cannot_produce_a_misleading_result(self):
        for needle in ("A lease that has run out proves nothing until the gate has evaluated it", "Both prompts now stop when the",
                       "dispatch the gate first", "Three attempts per PR, same head never twice", "attempt **2/3**", "kept and counted"):
            self.assertIn(needle, RUNBOOK, needle)

    def test_nothing_is_activated_by_this_change(self):
        self.assertIs(g.CONFIG["real_handoff_enabled"], False)
        self.assertEqual(g.CONFIG["max_attempts"], 3)


if __name__ == "__main__":
    unittest.main()
