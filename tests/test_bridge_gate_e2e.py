"""The disposable end-to-end test: the synthetic bug is real and small, the branch script is safe, the runbook says what it must.

No network, no GitHub, no AI. Runs the fixture and the script in temporary directories; creates no Routine, label or variable.
"""

import os
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


MARKER = "DISPOSABLE_E2E_FIXTURE"
BRANCH_PREFIX = "claude/e2e-synthetic-"


def branch_name(env, git_branch=""):
    """The branch the suite is really running on: the PR's head branch in GitHub Actions, else the checked-out branch ('' if unknown)."""
    if env.get("GITHUB_HEAD_REF"):
        return env["GITHUB_HEAD_REF"]
    if env.get("GITHUB_REF_TYPE") == "branch" and env.get("GITHUB_REF_NAME"):
        return env["GITHUB_REF_NAME"]
    return git_branch


def local_branch(root):
    try:
        return subprocess.run(["git", "branch", "--show-current"], cwd=root, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def fixture_leaks(root, branch):
    """What of the synthetic fixture is present under `root` unless this really is a disposable branch.

    The marker travels with the fixture, so on its own it proves nothing (a merged or cherry-picked fixture would carry it onto main).
    The fixture is allowed only with the marker AND a branch named claude/e2e-synthetic-*; anywhere else, or if the branch is unknown, it is a leak."""
    root = pathlib.Path(root)
    if (root / "synthetic" / MARKER).exists() and branch.startswith(BRANCH_PREFIX):
        return []
    leaks = []
    if (root / "synthetic").exists():
        leaks.append("synthetic/")
    if list((root / "tests").glob("test_synthetic*")):
        leaks.append("tests/test_synthetic*")
    return leaks


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
        """Not importable and not discovered here: only a marked throwaway branch (claude/e2e-synthetic-*) gets real .py files."""
        self.assertEqual(list(E2E.rglob("*.py")), [])
        branch = branch_name(os.environ, local_branch(g.ROOT))
        leaks = fixture_leaks(g.ROOT, branch)
        if (g.ROOT / "synthetic" / MARKER).exists() and not leaks:
            self.skipTest("disposable branch %s: the fixture is there on purpose and the branch is never merged" % branch)
        self.assertEqual(leaks, [], "synthetic fixture outside a disposable branch (branch=%r)" % branch)

    def test_the_marker_alone_never_disables_the_guard_and_the_branch_context_must_agree(self):
        """Codex finding on ceced1e: the marker is committed with the fixture, so a merged or cherry-picked fixture would carry it onto main."""
        base = pathlib.Path(tempfile.mkdtemp(prefix="e2e-inert-"))
        self.addCleanup(shutil.rmtree, base, True)
        self.assertEqual(fixture_leaks(base, "main"), [])                          # a clean tree
        (base / "synthetic").mkdir(); (base / "tests").mkdir()
        (base / "synthetic" / "pricing.py").touch()
        (base / "tests" / "test_synthetic_pricing.py").touch()
        both = ["synthetic/", "tests/test_synthetic*"]
        self.assertEqual(fixture_leaks(base, BRANCH_PREFIX + "x"), both)           # no marker: a leak even on an e2e branch name
        (base / "synthetic" / MARKER).touch()
        self.assertEqual(fixture_leaks(base, BRANCH_PREFIX + "f"), [])             # marker and the e2e branch: allowed
        for wrong in ("main", "", "claude/kind-cannon-qfijb5", "proto/bridge-gate", "claude/e2e-synthetic", "x/claude/e2e-synthetic-a"):
            self.assertEqual(fixture_leaks(base, wrong), both, wrong)              # marker on main, an unknown or any other branch: still a leak

    def test_the_branch_is_read_from_the_pr_head_in_actions_or_from_git_and_never_from_the_merge_ref(self):
        self.assertEqual(branch_name({"GITHUB_HEAD_REF": "claude/e2e-synthetic-f", "GITHUB_REF_NAME": "10/merge", "GITHUB_REF_TYPE": "branch"}), "claude/e2e-synthetic-f")
        self.assertEqual(branch_name({"GITHUB_REF_TYPE": "branch", "GITHUB_REF_NAME": "main"}, "ignored"), "main")
        self.assertEqual(branch_name({"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "v1"}, "claude/e2e-synthetic-a"), "claude/e2e-synthetic-a")
        self.assertEqual(branch_name({}, "claude/e2e-synthetic-a"), "claude/e2e-synthetic-a")
        self.assertEqual(branch_name({}), "")                                       # unknown: the guard stays strict
        self.assertEqual(branch_name({"GITHUB_HEAD_REF": "", "GITHUB_REF_NAME": "10/merge", "GITHUB_REF_TYPE": "branch"}), "10/merge")  # push-style refs are not e2e names

    def test_the_marker_is_written_by_the_branch_script_before_it_commits(self):
        text = SCRIPT.read_text()
        self.assertIn("Disposable bridge end-to-end fixture. Do not land it.", text)
        self.assertIn("> synthetic/%s" % MARKER, text)
        self.assertLess(text.index(MARKER), text.index("git add synthetic"))


class TwoStageFixtureTests(unittest.TestCase):
    """Test 4B's fixture: a first defect that CI and a local run both show, and a second that only GitHub Actions shows, and only once the first is fixed."""

    def tree(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="e2e-two-stage-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "synthetic").mkdir(); (root / "tests").mkdir()
        (root / "synthetic" / "__init__.py").touch(); (root / "tests" / "__init__.py").touch()
        for src, dst in (("pricing.py.txt", "synthetic/pricing.py"), ("test_pricing.py.txt", "tests/test_synthetic_pricing.py"),
                         ("receipt.py.txt", "synthetic/receipt.py"), ("test_receipt.py.txt", "tests/test_synthetic_receipt.py")):
            shutil.copy(E2E / "synthetic" / src, root / dst)
        return root

    def run_tests(self, root, in_actions):
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}
        if in_actions:
            env["GITHUB_ACTIONS"] = "true"
        return subprocess.run(["python3", "-W", "error", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                              cwd=root, capture_output=True, text=True, env=env)

    def fix_discount(self, root):
        src = root / "synthetic" / "pricing.py"
        src.write_text(src.read_text().replace("return price_cents * percent // 100", "return price_cents - price_cents * percent // 100"))

    def fix_receipt(self, root):
        src = root / "synthetic" / "receipt.py"
        src.write_text(src.read_text().replace("{cents / 100}", "{cents / 100:.2f}"))

    def test_stage_one_looks_the_same_locally_and_in_actions_and_hides_stage_two(self):
        root = self.tree()
        for in_actions in (False, True):
            bad = self.run_tests(root, in_actions)
            self.assertIn("FAILED (failures=4)", bad.stderr, in_actions)
            self.assertNotIn("Receipt", bad.stderr)

    def test_a_correct_first_fix_is_green_locally_but_red_in_actions(self):
        root = self.tree()
        self.fix_discount(root)
        self.assertEqual(self.run_tests(root, False).returncode, 0)          # what the correction session can run before it pushes
        red = self.run_tests(root, True)                                    # what CI then reports on the pushed head
        self.assertEqual(red.returncode, 1)
        self.assertIn("FAILED (failures=2)", red.stderr)
        self.assertIn("ReceiptLineTests", red.stderr)

    def test_the_second_fix_makes_it_green_everywhere(self):
        root = self.tree()
        self.fix_discount(root); self.fix_receipt(root)
        for in_actions in (False, True):
            self.assertEqual(self.run_tests(root, in_actions).returncode, 0, in_actions)

    def test_the_stage_two_files_are_covered_by_the_same_leak_guard(self):
        root = self.tree()
        self.assertEqual(fixture_leaks(root, "main"), ["synthetic/", "tests/test_synthetic*"])


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
        self.assertTrue((work / "synthetic" / MARKER).exists())

    def test_two_stage_adds_the_ci_only_defect_in_the_same_single_commit(self):
        work, run = self.repo()
        run("bridge-gate/e2e/make-synthetic-branch.sh", "4b-a", "two-stage", cwd=work)
        self.assertEqual(run("git", "branch", "--show-current", cwd=work).stdout.strip(), "claude/e2e-synthetic-4b-a")
        self.assertEqual(run("git", "rev-list", "--count", "origin/main..HEAD", cwd=work).stdout.strip(), "1")
        files = run("git", "show", "--name-only", "--format=", "HEAD", cwd=work).stdout.split()
        self.assertEqual(sorted(files), ["synthetic/DISPOSABLE_E2E_FIXTURE", "synthetic/__init__.py", "synthetic/pricing.py", "synthetic/receipt.py",
                                         "tests/test_synthetic_pricing.py", "tests/test_synthetic_receipt.py"])
        self.assertEqual(unit_tests(work).returncode, 1)

    def test_the_default_is_still_the_one_stage_fixture(self):
        work, run = self.repo()
        run("bridge-gate/e2e/make-synthetic-branch.sh", "a", cwd=work)
        self.assertFalse((work / "synthetic" / "receipt.py").exists())

    def test_it_refuses_an_unknown_second_argument(self):
        work, run = self.repo()
        self.assertNotEqual(subprocess.run([str(work / SCRIPT.relative_to(g.ROOT)), "a", "three-stage"], cwd=work, capture_output=True).returncode, 0)

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

    def test_test_3_is_documented_with_its_prerequisites_and_stop_conditions(self):
        sec = RUNBOOK[RUNBOOK.index("## Test 3: Stage 2 on a fresh PR"):RUNBOOK.index("## 11. Outcomes")]
        for needle in ("attempt 1/3, nothing reset", "DISPOSABLE_E2E_FIXTURE", "Branches `a` to `e` predate it", "**and** a real branch named `claude/e2e-synthetic-*`", "the marker travels with the fixture and proves nothing on its own", "One active Routine per label",
                       "at least 60 minutes of lease remain", "Address review findings (bridge attempt 1/3)", "changing only `synthetic/pricing.py`",
                       "a force-push", "`.github/**`, `bridge-gate/**` or `.claude/**`", "Nothing merges", "switch back to the Stage 1 Routine"):
            self.assertIn(needle, sec, needle)

    def test_test_4b_is_documented_with_its_scope_its_inconclusive_outcome_and_its_stop_conditions(self):
        section = RUNBOOK.split("## Test 4B")[1].split("## 11. Outcomes")[0]
        for needle in ("handoff_branch_prefix", "claude/e2e-synthetic-4b-", "HANDOFF_OUT_OF_SCOPE", "two-stage", "only on GitHub Actions",
                       "Address review findings (bridge attempt 1/3)", "Address review findings (bridge attempt 2/3)", "a third attempt",
                       "inconclusive", "`READY`", "BRIDGE_KILL_SWITCH=true", "Never label or touch PR #2", "human-only merge rule", "non-draft"):
            self.assertIn(needle, section, needle)
        self.assertIn("S21", (g.GATE / "SECURITY.md").read_text())
        self.assertIn("HANDOFF_OUT_OF_SCOPE", (g.GATE / "README.md").read_text())

    def test_nothing_is_activated_by_this_change(self):
        self.assertIs(g.CONFIG["real_handoff_enabled"], False)
        self.assertEqual(g.CONFIG["max_attempts"], 3)


if __name__ == "__main__":
    unittest.main()
