"""The lease-expiry watchdog: it only asks the gate to re-evaluate expired in-flight leases. No network, no GitHub, no AI.

watchdog.sh runs against the fake `gh`; the workflow is read as text. Nothing here schedules, dispatches or labels anything real.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

from . import gate_sim as g
from .test_bridge_gate_collect import FAKES, state_comment_body

WATCHDOG = g.GATE / "watchdog.sh"
YAML = (g.ROOT / ".github" / "workflows" / "bridge-watchdog.yml").read_text()
CODE = "\n".join(l for l in YAML.splitlines() if not l.lstrip().startswith("#"))
BOT = {"login": "github-actions[bot]", "type": "Bot"}
DISPATCH = "POST repos/o/r/actions/workflows/bridge-gate.yml/dispatches"


def attempt(status="in_flight", lease=g.NOW - 1, n=1):
    return {"n": n, "head": g.sha(n), "trigger": "k", "reason": "CODEX_FINDINGS", "at": g.NOW - 7200, "lease_expires": lease, "status": status}


def state(*attempts):
    return {"v": 1, "attempts": list(attempts), "seen": [], "last": None}


def comment(who, body):
    return {"user": who, "body": body}


class Fake:
    def __init__(self, prs):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="gate-watchdog-"))
        self.put("pulls", [{"number": n} for n in prs])
        for n in prs:
            self.put("issues_%d_comments" % n, [])

    def put(self, slug, payload):
        (self.dir / (slug + ".json")).write_text(json.dumps(payload))

    def state_on(self, n, st, who=BOT):
        self.put("issues_%d_comments" % n, [comment(who, state_comment_body(st))])

    def run(self, **extra):
        env = {"PATH": "%s:/usr/bin:/bin:/usr/local/bin" % FAKES, "HOME": str(self.dir), "FAKE_GH_DIR": str(self.dir),
               "REPO": "o/r", "DEFAULT_BRANCH": "main", "NOW": str(g.NOW)}
        env.update(extra)
        return subprocess.run(["bash", "--noprofile", "--norc", "-o", "pipefail", str(WATCHDOG)], env=env, capture_output=True, text=True)

    def writes(self):
        p = self.dir / "writes.log"
        return p.read_text().splitlines() if p.exists() else []

    def dispatched(self):
        return [int(re.search(r"inputs\[pr_number\]=(\d+)", w).group(1)) for w in self.writes() if w.startswith(DISPATCH)]


class WatchdogTests(unittest.TestCase):
    def fake(self, prs=(7,)):
        fx = Fake(prs)
        self.addCleanup(shutil.rmtree, fx.dir, True)
        return fx

    def test_an_expired_in_flight_lease_dispatches_the_gate_for_that_pr_only(self):
        fx = self.fake((7, 8, 9))
        fx.state_on(7, state(attempt()))
        fx.state_on(8, state(attempt(lease=g.NOW + 60)))
        out = fx.run()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(fx.writes(), [DISPATCH + " ref=main inputs[pr_number]=7"])

    def test_the_boundary_agrees_with_the_gate(self):
        """The gate waits while now < lease_expires and times out at now >= lease_expires; the watchdog uses the same edge."""
        for lease, timed_out in ((g.NOW + 1, False), (g.NOW, True), (g.NOW - 1, True)):
            fx = self.fake()
            fx.state_on(7, state(attempt(lease=lease)))
            fx.run()
            self.assertEqual(fx.dispatched(), [7] if timed_out else [], lease)
            d = g.run_gate(g.facts(state=state(attempt(lease=lease))))
            self.assertEqual(d["reason"] == "HANDOFF_TIMEOUT", timed_out, lease)

    def test_only_a_live_in_flight_attempt_qualifies(self):
        for st in (None, state(), state(attempt("superseded")), state(attempt("timed_out")), state(attempt("in_flight", lease=None))):
            fx = self.fake()
            if st is not None:
                fx.state_on(7, st)
            self.assertEqual(fx.run().returncode, 0)
            self.assertEqual(fx.writes(), [], st)

    def test_a_state_comment_not_written_by_the_bot_or_in_a_pair_or_corrupt_is_ignored(self):
        human = {"login": "octocat", "type": "User"}
        lookalike = {"login": "github-actions[bot]", "type": "User"}
        cases = {
            "human": [comment(human, state_comment_body(state(attempt())))],
            "lookalike": [comment(lookalike, state_comment_body(state(attempt())))],
            "two": [comment(BOT, state_comment_body(state(attempt())))] * 2,
            "corrupt": [comment(BOT, "<!-- bridge-gate-state:v1 {not json -->")],
            "array": [comment(BOT, "<!-- bridge-gate-state:v1 [1] -->")],
        }
        for name, comments in cases.items():
            fx = self.fake()
            fx.put("issues_7_comments", comments)
            out = fx.run()
            self.assertEqual((out.returncode, fx.writes()), (0, []), name)

    def test_at_most_five_dispatches_per_run_and_the_rest_wait_for_the_next_one(self):
        fx = self.fake(tuple(range(1, 9)))
        for n in range(1, 9):
            fx.state_on(n, state(attempt()))
        out = fx.run()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(fx.dispatched(), [1, 2, 3, 4, 5])
        self.assertEqual(fx.run(MAX_DISPATCH="2").returncode, 0)
        self.assertEqual(fx.dispatched()[5:], [1, 2])

    def test_stuck_prs_beyond_the_cap_take_turns_instead_of_the_same_five_every_hour(self):
        """Codex finding on efd40a9: the gate cannot clear these (e.g. persistent EVIDENCE_UNAVAILABLE), so they stay expired."""
        for stuck in (8, 12, 23):
            fx = self.fake(tuple(range(1, stuck + 1)))
            for n in range(1, stuck + 1):
                fx.state_on(n, state(attempt()))
            seen = set()
            for hour in range(-(-stuck // 5)):                      # ceil(stuck / 5) hourly runs
                before = len(fx.dispatched())
                out = fx.run(NOW=str(g.NOW + 3600 * hour))
                self.assertEqual(out.returncode, 0, out.stderr)
                batch = fx.dispatched()[before:]
                self.assertEqual(len(batch), min(5, stuck), (stuck, hour))
                self.assertEqual(len(set(batch)), len(batch), "no PR twice in one run")
                seen |= set(batch)
            self.assertEqual(seen, set(range(1, stuck + 1)), "every expired PR was dispatched within ceil(n/5) runs (%d stuck)" % stuck)

    def test_consecutive_runs_do_not_start_from_the_same_pr(self):
        fx = self.fake(tuple(range(1, 9)))
        for n in range(1, 9):
            fx.state_on(n, state(attempt()))
        firsts = []
        for hour in range(4):
            before = len(fx.dispatched())
            fx.run(NOW=str(g.NOW + 3600 * hour))
            firsts.append(fx.dispatched()[before])
        self.assertGreater(len(set(firsts)), 1, firsts)

    def test_rotation_never_exceeds_the_cap_or_dispatches_a_pr_that_is_not_expired(self):
        fx = self.fake(tuple(range(1, 10)))
        for n in range(1, 10):
            fx.state_on(n, state(attempt(lease=g.NOW + 600) if n % 3 == 0 else attempt()))
        for hour in range(6):
            before = len(fx.dispatched())
            fx.run(NOW=str(g.NOW + 3600 * hour) if hour else str(g.NOW))
            batch = fx.dispatched()[before:]
            self.assertLessEqual(len(batch), 5)
            if hour == 0:
                self.assertTrue(all(n % 3 for n in batch), batch)   # PRs 3, 6, 9 are still inside their lease

    def test_a_failed_dispatch_or_unreadable_pr_is_reported_but_does_not_stop_the_others(self):
        fx = self.fake((7, 8))
        fx.state_on(7, state(attempt()))
        fx.state_on(8, state(attempt()))
        (fx.dir / "fail_attempt").write_text("1")
        out = fx.run()
        self.assertEqual(out.returncode, 1)
        self.assertEqual(fx.dispatched(), [8])
        fx = self.fake((7, 8))
        fx.state_on(8, state(attempt()))
        (fx.dir / "fail.txt").write_text("issues_7_comments\n")
        out = fx.run()
        self.assertEqual((out.returncode, fx.dispatched()), (1, [8]))

    def test_an_unlistable_repository_or_bad_input_dispatches_nothing(self):
        fx = self.fake()
        fx.state_on(7, state(attempt()))
        (fx.dir / "fail.txt").write_text("pulls\n")
        self.assertEqual((fx.run().returncode, fx.writes()), (1, []))
        fx = self.fake()
        fx.state_on(7, state(attempt()))
        for bad in ({"REPO": "o/r; rm -rf /"}, {"DEFAULT_BRANCH": "../x"}, {"DEFAULT_BRANCH": ""}, {"MAX_DISPATCH": "x"}, {"NOW": "soon"}):
            self.assertEqual((fx.run(**bad).returncode, fx.writes()), (1, []), bad)

    def test_its_only_write_is_the_dispatch_of_the_gate_and_it_never_reads_anything_it_does_not_need(self):
        fx = self.fake()
        fx.state_on(7, state(attempt()))
        fx.run()
        self.assertTrue(all(w.startswith(DISPATCH) for w in fx.writes()))
        for call in (fx.dir / "calls.log").read_text().splitlines():
            self.assertRegex(call, r"^api (-X POST repos/o/r/actions/workflows/bridge-gate\.yml/dispatches|--paginate repos/o/r/(pulls\?|issues/7/comments))")
        text = WATCHDOG.read_text()
        self.assertNotRegex(re.sub(r"(?m)^\s*#.*", "", text), r"labels|merge|reviews|/git/|contents|\bpush\b|secrets|token")


class WatchdogWorkflowTests(unittest.TestCase):
    def test_permissions_are_pinned_and_carry_neither_merge_nor_administration_rights(self):
        block = re.search(r"(?m)^permissions:\n((?:  .*\n)+)", CODE).group(1)
        self.assertEqual(dict(re.findall(r"^  ([a-z-]+):\s*(\S+)", block, re.M)),
                         {"contents": "read", "pull-requests": "read", "actions": "write"})
        self.assertEqual(len(re.findall(r"(?m)^\s*permissions:", CODE)), 1)
        for bad in ("write-all", "contents: write", "pull-requests: write", "administration", "id-token", "checks: write", "issues: write"):
            self.assertNotIn(bad, CODE)

    def test_it_runs_hourly_or_by_hand_from_the_default_branch_and_is_inert_until_the_handoff_is_on(self):
        on_block = CODE.split("\non:", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual(re.findall(r"^  ([a-z_]+):", on_block, re.M), ["schedule", "workflow_dispatch"])
        self.assertEqual(re.findall(r"cron:\s*'([^']+)'", on_block), ["23 * * * *"])
        cond = re.sub(r"\s+", " ", CODE.split("    if: >-", 1)[1].split("runs-on:", 1)[0])
        for needle in ("github.repository == 'fcapiral-bot/ai-automation-public-test'",
                       "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)",
                       "vars.BRIDGE_KILL_SWITCH != 'true'",
                       "(github.event_name == 'workflow_dispatch' || vars.BRIDGE_HANDOFF_MODE == 'label')"):
            self.assertIn(needle, cond)

    def test_it_uses_only_the_builtin_token_no_pr_code_and_no_expression_in_a_script(self):
        self.assertEqual(set(re.findall(r"secrets\.(\w+)", CODE)), {"GITHUB_TOKEN"})
        self.assertNotRegex(CODE, r"pull_request|issue_comment|workflow_run|head\.sha|head_ref|ref:")
        self.assertEqual(re.findall(r"(?m)^\s+run: (.*)$", CODE), ["bridge-gate/watchdog.sh"])
        self.assertIn("persist-credentials: false", CODE)

    def test_only_the_watchdog_has_a_schedule_and_the_gate_still_has_none(self):
        for path in (g.ROOT / ".github" / "workflows").glob("*.yml"):
            code = "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("#"))
            scheduled = re.search(r"(?m)^\s*schedule:|cron:", code) is not None
            self.assertEqual(scheduled, path.name == "bridge-watchdog.yml", path.name)

    def test_it_is_small_and_names_the_script_it_runs(self):
        self.assertLess(len(YAML.splitlines()), 50)
        self.assertLess(len(WATCHDOG.read_text().splitlines()), 50)
        self.assertTrue(os.access(WATCHDOG, os.X_OK))


if __name__ == "__main__":
    unittest.main()
