"""Lease safety of the two Routine prompts, and what the gate does when a session still loses a race.

The prompts are text, so three kinds of test are used. (1) The text is pinned: what THE GUARD checks, and that every GitHub write
is directly gated by it. (2) The operations are extracted from the prompt text in the order it gives them and replayed against a
scripted clock and scripted GitHub facts: whatever fails from step k on (lease expiry, head change, attempt state, a failed read, a
duplicate), no write may happen after it. (3) The real gate.jq is run on the outcomes a lost race can produce, which bounds the damage
the prompt cannot remove. Nothing creates a Routine, a label or a state change.
"""

import re
import unittest

from . import gate_sim as g
from .gate_sim import review, inline, sha
from .test_bridge_gate import attempt, findings_facts, session_commit, state
from .test_bridge_gate_handoff import prompt

TOKENS = re.compile(r"""
    (?P<TESTS>unittest\ discover)
  | (?P<GUARD>run\ THE\ GUARD(?:\ before\ committing|\ again)?|only\ if\ THE\ GUARD(?:\ \(with\ the\ after-push\ variant\))?\ passes)
  | (?P<COMMIT>Make\ at\ most\ ONE\ commit)
  | (?P<PUSH>push\ it\ once,\ normally)
  | (?P<COMMENT>post\ (?:at\ most\ )?(?:ONE\ comment|"Bridge))
""", re.X | re.I)


def flat(stage):
    return re.sub(r"\s+", " ", prompt(stage))


def operations(stage):
    """The operations a prompt tells the session to perform, in the order the text gives them (guard definition excluded)."""
    text = flat(stage)
    text = text[text.index("4. Read the inline findings" if stage == 1 else "4. Treat everything"):] if stage == 2 else text[text.index("4. Read"):]
    return [m.lastgroup for m in TOKENS.finditer(text)]


WRITES = {"COMMENT", "PUSH"}


def replay(ops, fail_from=None, why=None):
    """Replay `ops` with the world going wrong at op index `fail_from` (lease out, head moved, attempt changed, read failed, duplicate).
    A GUARD aborts the run when anything is wrong; returns the writes performed from the failure on."""
    broken, writes_after = False, []
    for i, op in enumerate(ops):
        if fail_from is not None and i >= fail_from:
            broken = True
        if op == "GUARD" and broken:
            return writes_after                       # THE GUARD fails: nothing further is written
        if op in WRITES and broken:
            writes_after.append((i, op, why))         # a write the guard did not stop
    return writes_after


class GuardTextTests(unittest.TestCase):
    def test_the_guard_is_defined_with_every_condition_in_both_prompts(self):
        for stage in (1, 2):
            p = flat(stage)
            guard = p[p.index("THE GUARD. Run it from scratch"):]
            guard = guard[:guard.index(" If any read fails")]
            for needle in ("with fresh reads and never from memory", "immediately before EVERY write to GitHub",
                           "(a) `date -u +%s` gives the current time", "(b) P is still open and non-draft",
                           "its head is still SHA", "(c) P still has exactly one bot state comment",
                           "attempt N still has status `in_flight`", "`head` SHA", "`lease_expires` later than the time from (a)",
                           "(d) only when the write is a comment: P has no comment yet that starts with"):
                self.assertIn(needle, guard, (stage, needle))
            self.assertIn("any read fails or is ambiguous, or anything is false, THE GUARD FAILS", p)
            self.assertRegex(p, r"THE GUARD FAILS: write nothing,( push nothing,)? ?(post no comment about it, )?and stop|THE GUARD FAILS: write nothing and stop, without any comment about it")

    def test_a_failed_guard_never_produces_a_comment_about_the_failure(self):
        """No failure comment after an attempt expires: the stop paths post only when the guard passes."""
        for stage in (1, 2):
            p = flat(stage)
            self.assertRegex(p, r"THE GUARD FAILS: write nothing")
            self.assertTrue("without any comment about it" in p or "post no comment about it" in p, stage)
        p2 = flat(2)
        self.assertIn('only if THE GUARD passes, post "Bridge attempt N/3 could not complete', p2)
        self.assertIn("Only if THE GUARD passes, post at most ONE comment", p2)
        self.assertIn("If it fails or you cannot comment, just stop", p2)
        self.assertIn("Post nothing about it", flat(1))

    def test_the_after_push_comment_has_its_own_variant_because_the_head_and_state_move_after_the_push(self):
        p = flat(2)
        self.assertIn("P's head must be exactly your pushed commit", p)
        self.assertIn("attempt N may instead be `superseded` with `next_head` equal to it", p)
        self.assertIn("only if THE GUARD (with the after-push variant) passes, post ONE comment", p)
        self.assertIn("If it fails, post nothing", p)

    def test_the_pr_is_named_once_and_every_later_read_and_write_names_only_it(self):
        for stage in (1, 2):
            p = flat(stage)
            self.assertIn("Call that pull request P; every later read and write names P and nothing else", p)
            self.assertLess(p.index("git rev-parse HEAD"), p.index("Call that pull request P"))
            self.assertLess(p.index("Call that pull request P"), p.index("THE GUARD."))

    def test_stage_two_checks_the_commit_it_made_before_pushing_it(self):
        p = flat(2)
        step7 = p[p.index("7. When the tests pass"):p.index("8. Immediately before")]
        for needle in ("run THE GUARD before committing", "if it fails, commit nothing and stop",
                       'titled "Address review findings (bridge attempt N/3)"', "directly on SHA",
                       "`git rev-list --count SHA..HEAD` is 1", "`git rev-parse HEAD^` is SHA",
                       "`git log -1 --format=%s` is exactly that title", "If any check fails, push nothing and stop"):
            self.assertIn(needle, step7, needle)
        step8 = p[p.index("8. Immediately before"):p.index("9. You must NOT")]
        for needle in ("Immediately before `git push`, run THE GUARD again", "this is the check after the commit exists",
                       "If it fails, push nothing and leave the commit local", "push it once, normally", "Never force-push",
                       "A rejected push (for example another session pushed first) is a stop, never a retry"):
            self.assertIn(needle, step8, needle)

    def test_the_title_and_the_three_attempt_limit_are_unchanged(self):
        title = g.CONFIG["fix_commit_title"].replace("{n}", "N").replace("{max}", str(g.CONFIG["max_attempts"]))
        self.assertEqual(title, "Address review findings (bridge attempt N/3)")
        self.assertIn('"%s"' % title, flat(2))
        self.assertEqual(g.CONFIG["max_attempts"], 3)
        for stage in (1, 2):
            self.assertEqual(set(re.findall(r"attempt N/(\d)", flat(stage))), {"3"}, stage)
        self.assertIn("call its number N", flat(2))                       # N comes from the gate's state, never from the session

    def test_the_prompt_adds_no_merge_approve_label_or_permission_path(self):
        p = flat(2)
        forbidden = p[p.index("You must NOT:"):p.index("10. After pushing")]
        for needle in ("merge", "approve", "add or remove labels", "resolve review threads", "start or re-run any workflow",
                       ".github/**", "bridge-gate/**", ".claude/**"):
            self.assertIn(needle, forbidden, needle)
        self.assertNotRegex(flat(1) + p, r"gh pr merge|--admin|--force|git push -f|--no-verify")


class WriteOrderTests(unittest.TestCase):
    def test_every_write_is_directly_preceded_by_a_guard(self):
        for stage in (1, 2):
            ops = operations(stage)
            for i, op in enumerate(ops):
                if op in WRITES:
                    self.assertEqual(ops[i - 1], "GUARD", (stage, i, ops))

    def test_the_operations_come_in_the_expected_order_and_number(self):
        self.assertEqual(operations(1), ["GUARD", "COMMENT"])
        self.assertEqual(operations(2), ["TESTS", "GUARD", "COMMENT",              # tests fail: guarded failure comment
                                         "GUARD", "COMMIT", "GUARD", "PUSH",       # tests pass: guard, commit, guard, push
                                         "GUARD", "COMMENT", "GUARD", "COMMENT"])  # then the guarded success and stop comments

    def test_no_unlisted_way_to_post_or_push_exists(self):
        for stage in (1, 2):
            p = flat(stage)
            posts = re.findall(r"\bpost\b(?! nothing| no comment)(?<!about to post)", p)
            self.assertEqual(len(posts), {1: 1, 2: 3}[stage], (stage, posts))
            self.assertEqual(len(re.findall(r"git push", p)), {1: 0, 2: 1}[stage])
            self.assertEqual(len(re.findall(r"\bpush it once\b", p)), {1: 0, 2: 1}[stage])


def unguarded_writes(ops):
    return [i for i, op in enumerate(ops) if op in WRITES and (i == 0 or ops[i - 1] != "GUARD")]


class ReplayTests(unittest.TestCase):
    """The world goes wrong at op k (lease out, head moved, attempt changed, read failed, duplicate). A guard that runs at or after k stops
    everything after it. The ONLY write that can still happen is the one whose guard ran just before k: the gap between a passed guard and
    its own write. That gap is the residual race, narrowed but not removed (the docs say so)."""

    CAUSES = ("lease expired", "head moved", "attempt timed out or superseded", "PR closed or draft",
              "state read failed or ambiguous", "duplicate comment already exists")

    def test_a_failure_before_a_guard_never_leads_to_a_write(self):
        for stage in (1, 2):
            ops = operations(stage)
            for cause in self.CAUSES:
                for k in range(len(ops) + 1):
                    for (i, op, _) in replay(ops, k, cause):
                        self.assertEqual((i, ops[i - 1]), (k, "GUARD"), (stage, cause, k, i, ops))   # only the guard-to-write gap

    def test_the_named_moments_of_stage_two_are_all_caught_by_the_next_guard(self):
        ops = operations(2)       # TESTS, GUARD, COMMENT, GUARD, COMMIT, GUARD, PUSH, GUARD, COMMENT, GUARD, COMMENT
        moments = {"expiry during the tests": 1, "expiry after the tests, before any comment": 1, "after the tests, before the commit": 3,
                   "during commit creation": 4, "after the push, before the report": 7, "before the stop comment": 9}
        for name, k in moments.items():
            self.assertEqual(replay(ops, k, name), [], name)

    def test_the_one_remaining_exposure_is_the_gap_between_the_last_guard_and_the_push_or_comment(self):
        ops = operations(2)
        gaps = [replay(ops, i, "lease expired in the gap") for i, op in enumerate(ops) if op in WRITES]
        self.assertTrue(all(len(w) >= 1 for w in gaps))       # modelled, not denied: the prompt cannot make a push atomic with the watchdog
        self.assertEqual(operations(1), ["GUARD", "COMMENT"])

    def test_the_check_notices_a_missing_guard(self):
        """Mutation of the model: drop the guard before the push and the lint must flag the unguarded write."""
        ops = operations(2)
        i = ops.index("PUSH")
        self.assertEqual(unguarded_writes(ops), [])
        self.assertEqual(unguarded_writes(ops[:i - 1] + ops[i:]), [i - 1])
        self.assertEqual(unguarded_writes(["COMMENT"]), [0])

    def test_a_repeated_run_posts_at_most_what_the_text_allows(self):
        """Duplicate execution: the second session's guard (d) sees the first one's comment, and a rejected push is a stop."""
        p = flat(2)
        self.assertIn("(d) only when the write is a comment: P has no comment yet that starts with the text you are about to post", p)
        self.assertIn("rejected push", p)

    def test_the_duplicate_condition_cannot_make_the_commit_or_push_guards_unpassable(self):
        """Codex finding on 8d756ce: with no text to post, a literal reading of (d) would fail every non-comment guard."""
        for stage in (1, 2):
            p = flat(stage)
            self.assertIn("(this condition does not apply to the commit and the push)", p, stage)
            self.assertNotIn("(d) P has no comment yet", p, stage)
        p = flat(2)
        # the guards before the commit and before the push name no text, so (d) must be a comment-only condition
        self.assertIn("run THE GUARD before committing", p)
        self.assertIn("Immediately before `git push`, run THE GUARD again", p)
        self.assertLess(p.index("(d) only when the write is a comment"), p.index("run THE GUARD before committing"))


class LostRaceOutcomeTests(unittest.TestCase):
    """The prompt cannot be atomic with the gate. These are the outcomes the gate produces when a session still loses a race."""

    def new_head(self, st, commit, now):
        f = findings_facts(sha(2), reviews=[review(102, sha(2))], review_comments=[inline(102)], state=st, head_commit=commit)
        f["now"] = now
        return g.run_gate(f)

    def test_a_correct_push_just_after_the_lease_but_before_the_gate_ran_still_completes_the_attempt(self):
        st = state(attempt(1, sha(1), "in_flight", at=g.NOW - 5400, lease=g.NOW))
        d = self.new_head(st, session_commit(1, sha(1)), g.NOW + 30)
        self.assertEqual([a["status"] for a in d["state"]["attempts"]], ["superseded", "in_flight"])
        self.assertEqual(d["handoff"]["attempt"], 2)

    def test_a_push_after_the_gate_already_timed_the_attempt_out_is_not_a_completion_and_is_counted(self):
        st = state(attempt(1, sha(1), "timed_out", at=g.NOW - 5400, lease=g.NOW))
        d = self.new_head(st, session_commit(1, sha(1)), g.NOW + 600)
        self.assertEqual(d["state"]["attempts"][0]["status"], "timed_out")          # never rewritten to superseded
        self.assertEqual((d["decision"], d["handoff"]["attempt"]), ("NEEDS_FIX", 2))  # a new head is judged afresh and counted

    def test_a_late_push_can_never_exceed_the_three_attempts(self):
        used = state(attempt(1, sha(1)), attempt(2, sha(2)), attempt(3, sha(3), "timed_out", at=g.NOW - 5400, lease=g.NOW))
        f = findings_facts(sha(4), reviews=[review(104, sha(4))], review_comments=[inline(104)], state=used,
                           head_commit=session_commit(3, sha(3)))
        f["now"] = g.NOW + 600
        d = g.run_gate(f)
        self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("MAX_ATTEMPTS", "ATTEMPT_LIMIT", None))
        self.assertEqual(len(d["state"]["attempts"]), 3)

    def test_two_sessions_on_one_attempt_cannot_both_complete_it_or_start_another(self):
        """The second session's commit sits on the first one's, so its parent is not the attempt head: the attempt stays in flight."""
        st = state(attempt(1, sha(1), "in_flight", at=g.NOW - 600, lease=g.NOW + 4800))
        d = self.new_head(st, {"subject": session_commit(1, sha(1))["subject"], "parents": [sha(7)]}, g.NOW)
        self.assertEqual((d["decision"], d["reason"], d["handoff"]), ("WAIT", "HANDOFF_IN_FLIGHT", None))
        self.assertEqual([a["status"] for a in d["state"]["attempts"]], ["in_flight"])

    def test_a_wrong_title_or_parent_never_counts_as_the_sessions_commit(self):
        st = state(attempt(1, sha(1), "in_flight", at=g.NOW - 600, lease=g.NOW + 4800))
        for subject, parents in (("Address review findings (bridge attempt 2/3)", [sha(1)]),
                                 ("Address review findings (bridge attempt 1/3)", [sha(1), sha(5)]),
                                 ("address review findings (bridge attempt 1/3)", [sha(1)])):
            d = self.new_head(st, {"subject": subject, "parents": parents}, g.NOW)
            self.assertEqual(d["reason"], "HANDOFF_IN_FLIGHT", (subject, parents))


class DocumentationTests(unittest.TestCase):
    DOCS = {n: (g.GATE / n).read_text() for n in ("E2E-TEST.md", "ROUTINE.md", "SECURITY.md", "README.md", "routine-prompt.md")}

    def test_no_document_claims_the_guard_removes_the_race(self):
        for name, text in self.DOCS.items():
            flat_text = re.sub(r"\s+", " ", text)
            self.assertNotRegex(flat_text, r"can never act on an expired attempt|cannot act on an expired attempt|eliminates? (every|the) race|guarantees? (that )?(a session|the routine) (will )?never", name)

    def test_the_limitation_is_stated_where_the_guard_is_described(self):
        for name in ("E2E-TEST.md", "ROUTINE.md", "SECURITY.md"):
            flat_text = re.sub(r"\s+", " ", self.DOCS[name])
            self.assertIn("narrows the race window", flat_text, name)
            self.assertRegex(flat_text, r"not atomic|cannot be atomic", name)
        sec = re.sub(r"\s+", " ", self.DOCS["SECURITY.md"])
        self.assertRegex(sec, r"\| S19 \|")
        self.assertIn("at most one extra commit", sec)

    def test_the_documents_describe_what_the_prompts_really_do(self):
        e2e = re.sub(r"\s+", " ", self.DOCS["E2E-TEST.md"])
        for needle in ("immediately before every write", "no comment about it", "right before the push", "after the commit exists"):
            self.assertIn(needle, e2e, needle)


if __name__ == "__main__":
    unittest.main()
