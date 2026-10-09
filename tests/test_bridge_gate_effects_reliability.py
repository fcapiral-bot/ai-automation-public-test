"""Reliability of the gate's writes (Codex findings 2 and 3 on b1bbf60), against a fake GitHub. No network, no AI.

Finding 2: a decision made for an old head must never be recorded or announced.
Finding 3: a transient API failure must not silently lose a notification or a label cleanup, and a re-run must be safe.

The workflow's order is: guard -> remove label (real mode) -> notify -> persist state -> add label (real mode). Idempotent effects come
first, so a failed write stops the run before the transition is recorded and a re-run repeats it; only the label add follows the state
commit (write-ahead). Notices carry a key (head:decision:reason), so a re-run whose notice already landed does not post it again.
"""

import json
import re
import unittest

from . import gate_sim as g
from .gate_sim import CODEX, HUMAN, NOW, run_gate, sha, summary
from .test_bridge_gate_ci_wakeup import world
from .test_bridge_gate_collect import BOT, Fixture, state_comment_body, user
from .test_bridge_gate_workflow import (COLLECT, DECIDE, GUARD, NOTIFY, PERSIST, PIPELINE, PRE_ADD, REAL, REMOVE, Runner,
                                        findings_fixture)

H1, H2 = sha(1), sha(2)
LABEL_DELETE = "DELETE repos/o/r/issues/7/labels/bridge%3Aneeds-fix"
REAL_ENV = {"HANDOFF_MODE": "label"}


def move_head(fx, n=2):
    pr = json.loads((fx.dir / "pulls_7.json").read_text()); pr["head"]["sha"] = sha(n); fx.put("pulls_7", pr)


def fail_write(fx, attempt, status=502):
    (fx.dir / "fail_attempt").write_text(str(attempt)); (fx.dir / "fail_status").write_text(str(status))


def state_of(runner):
    body = next(b.read_text() for b in runner.bodies() if b.read_text().startswith("<!-- bridge-gate-state:v1 "))
    return json.loads(body.splitlines()[0][len("<!-- bridge-gate-state:v1 "):-len(" -->")])


def notice_bodies(runner):
    return [b.read_text() for b in runner.bodies() if b.read_text().startswith("<!-- bridge-gate-notice:v1 ")]


def kinds(fx):
    return [w.split()[0] + (" label" if "/labels" in w else "") for w in fx.writes()]


def add_comment(fx, cid, who, body):
    comments = json.loads((fx.dir / "issues_7_comments.json").read_text())
    comments.append({"id": cid, "user": who, "body": body}); fx.put("issues_7_comments", comments)


def lease_world(state, label=True):
    """A PR whose in-flight attempt's lease has expired and which still carries the handoff label."""
    fx = Fixture(H1)
    pr = json.loads((fx.dir / "pulls_7.json").read_text()); pr["labels"] = [{"name": "bridge:needs-fix", "id": 9}] if label else []; fx.put("pulls_7", pr)
    fx.put("issues_7_comments", [{"id": 4242, "user": BOT, "body": state_comment_body(state)}])
    fx.put("commits_SHA_check-runs", {"total_count": 0, "check_runs": []})
    return fx


def attempt(n, head, status="superseded", lease=NOW - 5_000):
    return {"n": n, "head": head, "trigger": "x", "reason": "CODEX_FINDINGS", "at": NOW - 10_000, "lease_expires": lease, "status": status}


EXPIRED = {"v": 1, "attempts": [attempt(1, H1, "in_flight", NOW - 1)], "seen": [], "last": None}
REAL_ORDER = PRE_ADD + [REAL]


class StaleHeadBeforeWritesTests(unittest.TestCase):
    """Finding 2."""

    def scenarios(self):
        ready = world()                                                       # READY: notice + state
        blocked = findings_fixture()                                          # BLOCKED / HANDOFF_LOCKED (real mode, lock closed): notice + state
        needs_fix = findings_fixture()                                        # NEEDS_FIX: state (+ label in real mode)
        return (("READY", Runner(ready), {}), ("BLOCKED", Runner(blocked, HANDOFF_MODE="label"), REAL_ENV), ("NEEDS_FIX", Runner(needs_fix), {}))

    def test_a_head_change_after_the_decision_stops_every_kind_of_decision_before_any_write(self):
        for label, r, env in self.scenarios():
            r.run(steps=[COLLECT, DECIDE], **env)
            expected = {"READY": "READY", "BLOCKED": "BLOCKED", "NEEDS_FIX": "NEEDS_FIX"}[label]
            self.assertEqual(r.decision()["decision"], expected)
            move_head(r.fx)
            out = r.run(steps=[GUARD, REMOVE, NOTIFY, PERSIST, REAL], **env)
            self.assertEqual(list(out), [GUARD], label)
            self.assertIn("refusing a stale decision", out[GUARD].stderr)
            self.assertEqual(r.fx.writes(), [], label)

    def test_a_head_change_between_the_guard_and_each_write_is_caught_by_that_writes_own_guard(self):
        fx = world(); r = Runner(fx)
        run = r.run(steps=[COLLECT, DECIDE, GUARD])
        self.assertEqual(r.decision()["decision"], "READY")
        move_head(fx)
        for step in (NOTIFY, PERSIST):
            out = r.run(steps=[step])
            self.assertNotEqual(out[step].returncode, 0, step)
            self.assertIn("refusing a stale decision", out[step].stderr)
        self.assertEqual(fx.writes(), [])
        fx = findings_fixture(); r = Runner(fx, unlock=True, HANDOFF_MODE="label")
        r.run(steps=[COLLECT, DECIDE, GUARD], **REAL_ENV); move_head(fx)
        self.assertNotEqual(r.run(steps=[REAL], **REAL_ENV)[REAL].returncode, 0)
        self.assertEqual(fx.writes(), [])

    def test_an_unreadable_head_refuses_a_decision_that_would_write(self):
        fx = world(); r = Runner(fx); r.run(steps=[COLLECT, DECIDE]); fx.fail("pulls_7")
        out = r.run(steps=[GUARD, NOTIFY, PERSIST])
        self.assertEqual(list(out), [GUARD]); self.assertIn("cannot re-read", out[GUARD].stderr)
        self.assertEqual(fx.writes(), [])

    def test_a_decision_that_writes_nothing_and_an_evidence_error_need_no_head_read(self):
        fx = world(); fx.fail("pulls_7_reviews"); r = Runner(fx)
        r.run(steps=[COLLECT, DECIDE])
        d = r.decision()
        self.assertEqual((d["decision"], d["reason"], d["notify"], d["state_changed"]), ("BLOCKED", "EVIDENCE_UNAVAILABLE", False, False))
        fx.fail("pulls_7", "pulls_7_reviews")
        out = r.run(steps=PIPELINE[2:])
        self.assertEqual([p.returncode for p in out.values()], [0] * len(out))
        self.assertEqual(fx.writes(), [])

    def test_a_refused_stale_run_leaves_the_new_heads_evaluation_untouched(self):
        fx = world(); r = Runner(fx); r.run(steps=[COLLECT, DECIDE]); move_head(fx)
        r.run(steps=[GUARD])
        fx2 = world(head=H2, codex_clean=False)                               # the new head: nothing reviewed yet
        r2 = Runner(fx2)
        run = r2.run()
        self.assertEqual(r2.decision()["decision"], "WAIT")
        self.assertEqual([n for n in notice_bodies(r2) if "READY" in n], [])


class NotificationReliabilityTests(unittest.TestCase):
    """Finding 3: notices."""

    def test_a_failed_notice_stops_before_the_state_commit_and_a_rerun_loses_nothing(self):
        for status in (502, 403, 500):
            fx = world(); fail_write(fx, 1, status); r = Runner(fx)
            out = r.run()
            self.assertEqual(list(out)[-1], NOTIFY, status); self.assertNotEqual(out[NOTIFY].returncode, 0)
            self.assertEqual(fx.writes(), [], status)                         # no notice, and NO state committed
            fx2 = world(); r2 = Runner(fx2)                                   # GitHub is unchanged: the same event is re-run
            out2 = r2.run()
            self.assertEqual([p.returncode for p in out2.values()], [0] * len(out2))
            self.assertEqual(kinds(fx2), ["POST", "POST"], status)             # the notice, then the state
            self.assertEqual(r2.decision()["decision"], "READY")
            self.assertEqual(len(notice_bodies(r2)), 1)

    def test_a_notice_that_landed_before_a_failed_state_write_is_not_posted_again(self):
        fx = world(); fail_write(fx, 2); r = Runner(fx)                       # write 1 = notice (ok), write 2 = state (502)
        out = r.run()
        self.assertEqual(list(out)[-1], PERSIST); self.assertNotEqual(out[PERSIST].returncode, 0)
        self.assertEqual(kinds(fx), ["POST"])                                  # only the notice landed
        landed = notice_bodies(r)[0]
        fx2 = world(); add_comment(fx2, 5001, BOT, landed)                     # GitHub holds that notice, but no state comment
        r2 = Runner(fx2)
        out2 = r2.run()
        self.assertEqual([p.returncode for p in out2.values()], [0] * len(out2))
        d = r2.decision()
        self.assertEqual((d["decision"], d["notify"], d["state_changed"]), ("READY", False, True))
        self.assertEqual(kinds(fx2), ["POST"])                                 # just the state; no second notice
        self.assertEqual(notice_bodies(r2), [])
        self.assertEqual(state_of(r2)["last"]["decision"], "READY")

    def test_a_forged_notice_marker_does_not_suppress_the_notice(self):
        key = "%s:READY:CODEX_CLEAN_AND_CI_PASSING" % H1
        for who in (user(HUMAN), {"login": "github-actions[bot]", "id": 1, "type": "User"}):   # a person, or a look-alike that is not a Bot
            fx = world(); add_comment(fx, 5002, who, "<!-- bridge-gate-notice:v1 %s -->\nforged" % key)
            r = Runner(fx); r.run()
            self.assertTrue(r.decision()["notify"], who); self.assertEqual(len(notice_bodies(r)), 1)

    def test_a_notice_is_keyed_by_head_decision_and_reason_and_never_repeated_for_the_same_key(self):
        fx = world(); r = Runner(fx); r.run()
        note = notice_bodies(r)[0]
        self.assertEqual(note.splitlines()[0], "<!-- bridge-gate-notice:v1 %s:READY:CODEX_CLEAN_AND_CI_PASSING -->" % H1)
        fx2 = world(state=state_of(r)); add_comment(fx2, 5003, BOT, note)      # everything already done; the event arrives again
        r2 = Runner(fx2); r2.run()
        self.assertEqual((r2.decision()["notify"], fx2.writes()), (False, []))
        fx3 = world(head=H2, other_commit=H1, state=state_of(r)); add_comment(fx3, 5003, BOT, note)   # a NEW head earns a new notice
        r3 = Runner(fx3); r3.run()
        self.assertTrue(r3.decision()["notify"])
        self.assertEqual(notice_bodies(r3)[0].splitlines()[0], "<!-- bridge-gate-notice:v1 %s:READY:CODEX_CLEAN_AND_CI_PASSING -->" % H2)

    def test_the_collector_reads_notice_keys_only_from_the_gates_own_bot_comments(self):
        fx = Fixture(H1)
        fx.put("issues_7_comments", [
            {"id": 1, "user": BOT, "body": "<!-- bridge-gate-notice:v1 %s:READY:X -->\nbody" % H1},
            {"id": 2, "user": user(HUMAN), "body": "<!-- bridge-gate-notice:v1 forged:READY:X -->\nbody"},
            {"id": 3, "user": BOT, "body": "<!-- bridge-gate-state:v1 {\"v\":1,\"attempts\":[],\"seen\":[],\"last\":null} -->"},
            {"id": 4, "user": BOT, "body": "an ordinary comment"}])
        self.assertEqual(fx.collect()["notices"], ["%s:READY:X" % H1])

    def test_pure_gate_notify_is_idempotent_on_the_notice_key_and_malformed_notices_fail_closed(self):
        f = g.facts(H1, issue_comments=[summary(H1)], check_runs=[g.check("unit-tests")])
        key = "%s:READY:CODEX_CLEAN_AND_CI_PASSING" % H1
        d = run_gate(f)
        self.assertEqual((d["notice_key"], d["notify"]), (key, True))
        self.assertFalse(run_gate({**f, "notices": [key]})["notify"])
        self.assertTrue(run_gate({**f, "notices": ["other"]})["notify"])
        self.assertTrue(run_gate(f)["notify"])                                  # a facts file without `notices` behaves as before
        for bad in ("x", {"a": 1}, 5, None):
            d = run_gate({**f, "notices": bad})
            self.assertEqual((d["decision"], d["reason"], d["notify"], d["state_changed"]), ("BLOCKED", "EVIDENCE_UNAVAILABLE", False, False), bad)


class LabelRemovalReliabilityTests(unittest.TestCase):
    """Finding 3: label cleanup."""

    def run_real(self, fx, steps=REAL_ORDER):
        r = Runner(fx, unlock=True, HANDOFF_MODE="label")
        return r, r.run(steps=steps, **REAL_ENV)

    def test_the_timeout_flow_removes_the_label_then_notifies_then_commits_in_that_order(self):
        fx = lease_world(EXPIRED); r, out = self.run_real(fx)
        self.assertEqual([p.returncode for p in out.values()], [0] * len(out), {k: v.stderr for k, v in out.items()})
        d = r.decision()
        self.assertEqual((d["decision"], d["reason"], d["clear_label"], d["notify"]), ("BLOCKED", "HANDOFF_TIMEOUT", True, True))
        self.assertEqual([w.split(" body")[0] for w in fx.writes()], [LABEL_DELETE, "POST repos/o/r/issues/7/comments", "PATCH repos/o/r/issues/comments/4242"])

    def test_a_failed_label_removal_stops_the_run_before_notice_and_state_and_a_rerun_repeats_everything(self):
        for status in (502, 500, 403, 422):                                    # any error but "already gone" must be loud
            fx = lease_world(EXPIRED); fail_write(fx, 1, status); r, out = self.run_real(fx)
            self.assertEqual(list(out)[-1], REMOVE, status); self.assertNotEqual(out[REMOVE].returncode, 0, status)
            self.assertIn("label removal failed", out[REMOVE].stderr, status)
            self.assertEqual(fx.writes(), [], status)                          # no notice, no state
            fx2 = lease_world(EXPIRED); r2, out2 = self.run_real(fx2)          # nothing was committed, so the same event is re-run
            self.assertEqual([p.returncode for p in out2.values()], [0] * len(out2), status)
            self.assertEqual(fx2.writes()[0], LABEL_DELETE); self.assertEqual(len(fx2.writes()), 3)

    def test_only_http_404_is_tolerated_when_the_label_is_already_gone(self):
        fx = lease_world(EXPIRED, label=False); fail_write(fx, 1, 404); r, out = self.run_real(fx)
        self.assertEqual([p.returncode for p in out.values()], [0] * len(out), {k: v.stderr for k, v in out.items()})
        self.assertEqual(kinds(fx), ["POST", "PATCH"])                         # notice + state still happen

    def test_a_failure_after_the_removal_succeeded_is_recovered_by_a_rerun_with_the_removal_repeated(self):
        fx = lease_world(EXPIRED); fail_write(fx, 2); r, out = self.run_real(fx)    # write 1 = DELETE ok, write 2 = notice (502)
        self.assertEqual(list(out)[-1], NOTIFY); self.assertEqual(fx.writes(), [LABEL_DELETE])
        fx2 = lease_world(EXPIRED, label=False); fail_write(fx2, 1, 404)            # the label is gone: DELETE answers 404, which is fine
        r2, out2 = self.run_real(fx2)
        self.assertEqual([p.returncode for p in out2.values()], [0] * len(out2))
        self.assertEqual(kinds(fx2), ["POST", "PATCH"]); self.assertEqual(len(notice_bodies(r2)), 1)
        self.assertEqual(r2.decision()["decision"], "BLOCKED")

    def test_an_unreadable_lock_fails_the_removal_instead_of_silently_skipping_it(self):
        fx = lease_world(EXPIRED); fx.fail("contents_bridge-gate_config.json"); r, out = self.run_real(fx)
        self.assertEqual(list(out)[-1], REMOVE); self.assertNotEqual(out[REMOVE].returncode, 0)
        self.assertIn("cannot read the activation lock", out[REMOVE].stderr)
        self.assertEqual(fx.writes(), [])

    def test_a_definitively_closed_lock_means_nothing_to_remove(self):
        for how in ("false", "missing"):
            fx = lease_world(EXPIRED); r = Runner(fx, HANDOFF_MODE="label")      # Runner default lock: closed on the default branch
            if how == "missing":
                (fx.dir / "contents_bridge-gate_config.json.json").unlink()      # HTTP 404: no config on the default branch
            out = r.run(steps=[COLLECT, DECIDE, GUARD, REMOVE], **REAL_ENV)
            self.assertEqual(out[REMOVE].returncode, 0, how); self.assertIn("locked off", out[REMOVE].stdout)
            self.assertFalse([w for w in fx.writes() if "labels" in w], how)

    def test_removal_and_add_do_nothing_and_read_nothing_when_the_decision_has_no_label_work(self):
        fx = world(); r = Runner(fx, unlock=True, HANDOFF_MODE="label")
        out = r.run(steps=[COLLECT, DECIDE, GUARD, REMOVE, REAL], **REAL_ENV)
        self.assertEqual([p.returncode for p in out.values()], [0] * 5)
        later = [c for c in fx.calls() if "contents/bridge-gate" in c or "/labels" in c]
        self.assertEqual(later[-1:] and [c for c in later if "/labels" in c], [])          # no label call
        self.assertEqual(sum("contents/bridge-gate/config.json" in c for c in fx.calls()), 1)   # only the collector's decision-time read

    def test_bad_input_is_refused_by_label_sh(self):
        import subprocess, os
        fx = lease_world(EXPIRED); r = Runner(fx, unlock=True, HANDOFF_MODE="label"); r.run(steps=[COLLECT, DECIDE], **REAL_ENV)
        for bad in ("main;touch pwned", "$(touch pwned)", "", "../x"):
            env = {**r.base, "DEFAULT_BRANCH": bad}
            p = subprocess.run([str(g.GATE / "label.sh"), "remove"], cwd=r.work, env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 1, bad); self.assertFalse((r.work / "pwned").exists(), bad)
        p = subprocess.run([str(g.GATE / "label.sh"), "frobnicate"], cwd=r.work, env=r.base, capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)


class WriteAheadAndRepeatedDeliveryTests(unittest.TestCase):
    def test_a_failed_state_write_means_no_label_and_no_attempt_and_the_rerun_succeeds(self):
        fx = findings_fixture(); fail_write(fx, 1); r = Runner(fx, unlock=True, HANDOFF_MODE="label")
        out = r.run(steps=REAL_ORDER, **REAL_ENV)
        self.assertEqual(list(out)[-1], PERSIST); self.assertEqual(fx.writes(), [])
        fx2 = findings_fixture(); r2 = Runner(fx2, unlock=True, HANDOFF_MODE="label"); r2.run(steps=REAL_ORDER, **REAL_ENV)
        self.assertEqual(kinds(fx2), ["POST", "POST label"])                   # state first, label second
        self.assertEqual([(a["n"], a["status"]) for a in state_of(r2)["attempts"]], [(1, "in_flight")])

    def test_a_failed_label_add_after_the_state_commit_leaves_one_visible_in_flight_attempt_and_never_a_second_handoff(self):
        fx = findings_fixture(); fail_write(fx, 2); r = Runner(fx, unlock=True, HANDOFF_MODE="label")   # write 1 = state ok, write 2 = label (502)
        out = r.run(steps=REAL_ORDER, **REAL_ENV)
        self.assertEqual(list(out)[-1], REAL); self.assertNotEqual(out[REAL].returncode, 0)       # the run is RED: not silent
        self.assertIn("adding the label failed", out[REAL].stderr)
        self.assertEqual(kinds(fx), ["POST"])
        saved = state_of(r)
        fx2 = findings_fixture(); fx2.put("issues_7_comments", [{"id": 4242, "user": BOT, "body": state_comment_body(saved)}])
        r2 = Runner(fx2, unlock=True, HANDOFF_MODE="label"); out2 = r2.run(steps=REAL_ORDER, **REAL_ENV)
        d = r2.decision()
        self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("WAIT", "HANDOFF_IN_FLIGHT", None))   # never a second attempt
        self.assertEqual(len(d["state"]["attempts"]), 1)
        self.assertEqual([w for w in fx2.writes() if "labels" in w], [])
        # Documented residual: the attempt stays in flight (no label was applied) until its lease ends; see SECURITY.md S14.

    def test_a_completed_handoff_event_delivered_again_never_hands_off_twice_and_then_changes_nothing(self):
        fx = findings_fixture(); r = Runner(fx, unlock=True, HANDOFF_MODE="label"); r.run(steps=REAL_ORDER, **REAL_ENV)
        saved = state_of(r)
        # the first redelivery only records that the attempt is in flight (decision NEEDS_FIX -> WAIT); no label, no second attempt
        fx2 = findings_fixture(); fx2.put("issues_7_comments", [{"id": 4242, "user": BOT, "body": state_comment_body(saved)}])
        r2 = Runner(fx2, unlock=True, HANDOFF_MODE="label"); out = r2.run(steps=REAL_ORDER, **REAL_ENV)
        self.assertEqual([p.returncode for p in out.values()], [0] * len(out))
        self.assertEqual(([w for w in fx2.writes() if "labels" in w], r2.decision()["handoff"], r2.decision()["duplicate"]), ([], None, True))
        self.assertEqual(len(state_of(r2)["attempts"]), 1)
        settled = state_of(r2)
        for _ in range(3):                                                         # every later delivery changes nothing at all
            fx3 = findings_fixture(); fx3.put("issues_7_comments", [{"id": 4242, "user": BOT, "body": state_comment_body(settled)}])
            r3 = Runner(fx3, unlock=True, HANDOFF_MODE="label"); out = r3.run(steps=REAL_ORDER, **REAL_ENV)
            self.assertEqual([p.returncode for p in out.values()], [0] * len(out))
            self.assertEqual(fx3.writes(), [])

    def test_simulate_mode_never_touches_labels_in_any_order(self):
        fx = lease_world(EXPIRED); r = Runner(fx); out = r.run()
        self.assertEqual([p.returncode for p in out.values()], [0] * len(out))
        self.assertFalse([w for w in fx.writes() if "labels" in w])
        self.assertIn("SIMULATED: would remove the handoff label", out["Handoff (SIMULATED, log only)"].stdout)


class AttemptHistoryIntactTests(unittest.TestCase):
    """The existing attempt history (the live PR has attempt 1 superseded and attempt 2 in flight) is never rewritten by these changes."""

    LIVE = {"v": 1, "attempts": [attempt(1, sha(11)) | {"next_head": sha(12)}, attempt(2, H1, "in_flight", NOW + 3000)], "seen": ["head:" + H1], "last": None}

    def test_non_transition_decisions_and_failed_runs_leave_every_attempt_byte_for_byte_unchanged(self):
        for label, setup in (("clean run", lambda fx: None), ("notice fails", lambda fx: fail_write(fx, 1)), ("state fails", lambda fx: fail_write(fx, 2))):
            fx = world(state=self.LIVE); setup(fx); r = Runner(fx); r.run()
            d = r.decision()
            self.assertEqual(d["state"]["attempts"], self.LIVE["attempts"], label)
            for b in r.bodies():
                if b.read_text().startswith("<!-- bridge-gate-state:v1 "):
                    self.assertEqual(state_of(r)["attempts"], self.LIVE["attempts"], label)

    def test_a_timeout_changes_only_the_in_flight_attempt_and_a_failed_then_repeated_run_adds_no_attempt(self):
        live = {**self.LIVE, "attempts": [self.LIVE["attempts"][0], attempt(2, H1, "in_flight", NOW - 1)]}
        fx = lease_world(live); fail_write(fx, 2); r = Runner(fx, unlock=True, HANDOFF_MODE="label"); r.run(steps=REAL_ORDER, **REAL_ENV)   # notice fails
        fx2 = lease_world(live); r2 = Runner(fx2, unlock=True, HANDOFF_MODE="label"); r2.run(steps=REAL_ORDER, **REAL_ENV)
        atts = state_of(r2)["attempts"]
        self.assertEqual(atts[0], live["attempts"][0])                           # attempt 1 untouched
        self.assertEqual((len(atts), atts[1]["n"], atts[1]["status"]), (2, 2, "timed_out"))
        self.assertEqual({k: v for k, v in atts[1].items() if k != "status"}, {k: v for k, v in live["attempts"][1].items() if k != "status"})

    def test_the_three_attempt_limit_holds_across_failed_and_repeated_runs(self):
        used = {"v": 1, "attempts": [attempt(1, sha(11)), attempt(2, sha(12)), attempt(3, sha(13))], "seen": [], "last": None}
        fx = world(state=used, conclusion="failure"); fail_write(fx, 1); r = Runner(fx); r.run()
        fx2 = world(state=used, conclusion="failure"); r2 = Runner(fx2); r2.run()
        d = r2.decision()
        self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("MAX_ATTEMPTS", "ATTEMPT_LIMIT", None))
        self.assertEqual(len(d["state"]["attempts"]), 3)
        self.assertEqual(len(notice_bodies(r2)), 1)


class DocumentationAndScopeTests(unittest.TestCase):
    DOCS = {p: (g.GATE / p).read_text() for p in ("README.md", "ROUTINE.md", "SECURITY.md", "routine-prompt.md")}

    def test_no_document_still_promises_an_automatic_lease_expiry_timeout(self):
        stale = (r"after the 90-minute lease it blocks and notifies", r"ends the attempt on its own after its lease",
                 r"attempt times out after\s+90 minutes", r"ends as `BLOCKED / HANDOFF_TIMEOUT`, never as a retry",
                 r"Routine failures end in `BLOCKED / HANDOFF_TIMEOUT` after the lease")
        for name, text in self.DOCS.items():
            flat = re.sub(r"\s+", " ", text)
            for phrase in stale:
                self.assertNotRegex(flat, re.sub(r"\\s\+", " ", phrase), (name, phrase))

    def test_the_documents_say_timeout_evaluation_needs_another_event_or_a_manual_dispatch(self):
        for name in ("README.md", "ROUTINE.md", "SECURITY.md"):
            flat = re.sub(r"\s+", " ", self.DOCS[name])
            self.assertIn("workflow_dispatch", flat, name)
            self.assertRegex(flat, r"another event|a later event|next event", name)
        self.assertRegex(re.sub(r"\s+", " ", self.DOCS["routine-prompt.md"]), r"next gate evaluation|another event")

    def test_the_missing_watchdog_is_recorded_as_a_blocker_for_unattended_activation(self):
        sec = re.sub(r"\s+", " ", self.DOCS["SECURITY.md"])
        self.assertRegex(sec, r"\| S15 \| \*\*High for unattended activation\*\* \|.*watchdog.*\*\*Open\. Documented blocker for unattended activation\.\*\*", "S15 row")
        self.assertRegex(sec, r"unattended")
        rt = re.sub(r"\s+", " ", self.DOCS["ROUTINE.md"])
        self.assertRegex(rt, r"watchdog")
        self.assertRegex(re.sub(r"\s+", " ", self.DOCS["SECURITY.md"].split("## Before the real handoff is enabled")[1]), r"watchdog")

    def test_the_duplicate_notification_scenario_is_documented(self):
        flat = re.sub(r"\s+", " ", self.DOCS["SECURITY.md"])
        self.assertRegex(flat, r"S14.*duplicate", "S14 documents what can still be duplicated")

    def test_no_scheduler_was_added_and_the_lock_and_ci_requirement_are_unchanged(self):
        for path in (g.ROOT / ".github" / "workflows").glob("*.yml"):
            code = "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("#"))
            self.assertNotRegex(code, r"(?m)^\s*schedule:|cron:", path.name)
        cfg = json.loads((g.GATE / "config.json").read_text())
        self.assertIs(cfg["real_handoff_enabled"], False); self.assertIs(cfg["require_ci"], True); self.assertEqual(cfg["max_attempts"], 3)


if __name__ == "__main__":
    unittest.main()
