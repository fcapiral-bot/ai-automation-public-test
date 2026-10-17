# The first live end-to-end test (NOT RUN, nothing here is active)

Goal: on a **disposable** pull request, GitHub starts Claude, Claude fixes a synthetic bug, Codex re-reviews the fix, and the gate
reaches `READY` **or stops safely**. Everything below is done **by hand by the owner**; no step is automated and nothing here has
been run. Abort at the first step that does not match what is written, and report what you saw instead of working around it.

The test PRs come from `claude/e2e-synthetic-*` branches. A routine pushes only to `claude/` branches by default and the prompt
refuses any other head branch, so PR #2 and every other PR stay out of reach. **Never label PR #2. Never merge a test PR.**

## Stop conditions (any one: stop, set `BRIDGE_KILL_SWITCH=true`, report)

Any charge or usage credit on the Claude or Codex side; a session that touches anything but the test PR's head branch; a merge, an
approval, a force-push, a `.github/**`, `bridge-gate/**` or `.claude/**` change by the Routine; more than one commit per attempt;
more than three attempts; a second session on the same attempt; an Actions run you did not expect.

## 1. Preconditions (`ROUTINE.md` section 0; all by hand)

Usage credits OFF org-wide; Routines and cloud sessions allowed; Claude GitHub App installed on this repository; Codex enabled for
this repository with purchased credits off (automatic review on every push was verified in the earlier private test repository);
the decisions on S10 and S18 (`SECURITY.md`) taken and written down; this change merged by you (both prompts now run THE GUARD before every write). Until S10 and S18 are decided
this test must not start: the Routine acts as your admin identity.

## 2. Labels and Routine (Stage 1 prompt)

Create the labels `bridge:needs-fix` and `bridge:usage-stop`. Create the Routine exactly as `ROUTINE.md` section 2 says, with the
**Stage 1 report-only** prompt. `BRIDGE_HANDOFF_MODE` stays unset and `real_handoff_enabled` stays `false`.

## 3. Two throwaway PRs with a synthetic bug

In a clone: `bridge-gate/e2e/make-synthetic-branch.sh a`, `git push -u origin claude/e2e-synthetic-a`, open a **non-draft** PR.
Repeat with `b`. The bug: `apply_discount` returns the discount instead of the discounted price, so `unit-tests` fails.
Expect (simulate mode): `ci` fails; the gate decides `NEEDS_FIX` and records **attempt 1/3** as `in_flight` with a 90-minute lease
(its log says `SIMULATED HANDOFF`); **no label**, **no Routine session**. Codex reviews each PR automatically and may also flag the
bug (not required: failing CI alone is enough to start an attempt).

**Observed 2026-10-14 (A = PR #4, head `7c1446a`; B = PR #5, head `6663826`):** `unit-tests` failed on both, Codex posted a P1 on
`synthetic/pricing.py:6` on both, and each gate state recorded attempt 1/3 (`CI_FAILING`, triggered by the `ci` completion, lease
ending 07:58Z). When the gate was next evaluated after 07:58Z each PR moved to `BLOCKED / HANDOFF_TIMEOUT`: attempt 1 is `timed_out`, one notice each, no label, nothing reset. **Both attempts are simulation
history and still count.** A and B have 2 of 3 attempts left, and the same head can never get another attempt.

## Attempt history and leases (read before step 5)

* **Simulation attempts are real attempts to the gate.** It records them exactly as it records a real handoff, in either mode, and counts
  every status (`in_flight`, `timed_out`, `superseded`) toward the limit of three. They are never reset silently: deleting a bot state
  comment is a person's deliberate act, it is **not** part of this test, and if you ever do it, write down the PR, the time and the old state.
* **A real-handoff test starts on a PR with no attempts.** That is a fresh PR (C), opened after the locks are open (step 7). A and B are
  only used for the free, no-handoff checks (steps 3 and 5) and are then closed unmerged.
* **Read the state comment before every manual label.** The first line of the bot's `bridge-gate-state:v1` comment lists each attempt and its
  `status` and `lease_expires` (Unix seconds). Add a label by hand only if no attempt is `in_flight`. A session on a PR with an
  in-flight, unexpired attempt acts on it, and its report would read as a real handoff when it is not.
* **A lease that has run out proves nothing until the gate has evaluated it.** After `lease_expires` the state still says `in_flight`
  until the gate runs again (another event, a manual dispatch, or the watchdog once the handoff is on). Both prompts now stop when the
  recorded lease is missing, not a number, or already past, and run THE GUARD (fresh reads of the PR head, the attempt's status and lease, and
  any comment with the same stable prefix `Bridge [<type>] PR #<n> attempt N/3 on <SHA7>:` written by the session's own authenticated GitHub user; a
  stranger's identical comment is ignored, and a session that cannot read its own identity posts nothing, which in a live test reads as a missing
  report: check the session transcript for that stop before suspecting the label route) immediately before every write: each comment, and the push (the guard runs right before the push, so after the commit exists). A failed
  guard writes nothing and posts no comment about it, so an expired attempt gets no failure comment. This narrows the race window; it is not
  atomic with the watchdog or the gate. A session can still pass the guard a moment before the lease ends and push a moment after (the
  gap between its last check and the push itself). The gate bounds that outcome: the attempt is already counted, the late commit is judged
  as a new head and counted too, and at most one extra commit can land per attempt, never a fourth attempt. In a test result, treat a
  comment or commit that appears within seconds of `lease_expires` as that race, not as a bug. Never read a result off
  an in-flight attempt whose lease has passed: dispatch the gate first and read the new state.
* **Three attempts per PR, same head never twice.** Plan for C: attempt 1 is the Stage 1 report-only run (it pushes nothing, so it times
  out and still counts), attempt 2 is the Stage 2 correction on a new head, attempt 3 is the spare. A timeout or a failed correction is a
  safe stop; `MAX_ATTEMPTS` ends the loop.

## 4. Cost check before any Claude run

Open claude.ai/settings/usage and the admin spend report. Note the current numbers. Credits must be $0 and stay $0.

## 5. Trigger and PR identity, by hand (checks `SECURITY.md` S17)

Precondition: the state comment of the PR shows **no attempt `in_flight`**. For A and B that is the case after the timeout above
(attempt 1 `timed_out`, last decision `BLOCKED / HANDOFF_TIMEOUT`). Real handoff stays off, so the gate adds no label and starts
nothing in this step.

1. Add the label `bridge:needs-fix` yourself to **A (PR #4) only**. Expect **one** Routine session (this proves a label event
   starts a session at all). Open it: its first command is `git rev-parse HEAD`, and it must equal A's head `7c1446a`, not B's. It finds
   no attempt in flight, so it must stop at step 2 and post, push and label nothing. Remove the label.
2. Then do the same for **B (PR #5)**, head `6663826`. Two sessions in total, each on its own head: that is the two-PR identity check.

If a session started on `main`, acted on the wrong PR, posted or pushed anything, or an attempt was `in_flight` when you labelled,
stop: the identity rule does not hold, or the precondition was not checked.

## 6. Cost check

The two sessions must show seat usage only. Any credit spend: stop.

## 7. Open both locks, for the throwaway PRs only

Close A and B **unmerged** first: they hold simulation history, and a closed PR decides `BLOCKED / PR_NOT_OPEN` and writes nothing.
Merge a PR that sets `real_handoff_enabled` to `true` in `bridge-gate/config.json` **and** set the variable `BRIDGE_HANDOFF_MODE` =
`label`. **Then** create a fresh PR C: `bridge-gate/e2e/make-synthetic-branch.sh c`, push, open it **non-draft**. Do not reuse A or B and do
not delete any state comment. When `ci` fails on C the gate (woken by the `ci` completion) decides `NEEDS_FIX` and records
**attempt 1/3** in flight (lease 90 minutes), then adds the label. If you see `BLOCKED / HANDOFF_LOCKED` instead, a lock is not open
and no attempt was recorded: stop and fix that. Note the `lease_expires` of this attempt.

## 8. Does a bot-applied label fire the Routine? (checks `SECURITY.md` S17, still the Stage 1 prompt)

Expect one session on C, finding C by its head, attempt 1/3 in flight and its lease not yet expired, posting ONE report-only comment
and pushing nothing. **If no session starts, the label route is unavailable: stop and report.** Do not create a token or an API trigger
(that is a separate decision).

## 9. The watchdog (checks `SECURITY.md` S15)

Stage 1 never finishes an attempt, so C's lease expires. Do not touch anything. Within the lease plus about two hours (the
watchdog runs hourly at minute 23), expect: a `bridge-watchdog` run that dispatched `bridge-gate`, then the state
`BLOCKED / HANDOFF_TIMEOUT` (attempt 1 `timed_out`, 2 attempts left), the label removed, one notice. If that has not happened after 3.5 hours, dispatch the gate by hand
and record the watchdog as **unverified**: the loop does not rely on it for safety, only for liveness.

## 10. The correction loop (Stage 2 prompt)

Switch the Routine to the **Stage 2 correction** prompt. Nothing is reset. The head of C that timed out can never get another attempt,
so a person pushes one harmless commit to `claude/e2e-synthetic-c` (for example a docstring change that leaves the bug in place; not an
empty commit, and not titled `Address review findings`). The new head wakes the gate. Expect, in order: attempt **2/3** recorded
(the timed-out attempt 1 is kept and counted); label; one session; **one commit** titled
`Address review findings (bridge attempt 2/3)` on `claude/e2e-synthetic-c` (it makes
`apply_discount` return `price_cents - price_cents * percent // 100`, and changes no workflow, no `bridge-gate/**`); `unit-tests`
passes; Codex reviews the new head by itself (👀, then 👍 or a review); the gate decides `READY` and posts its notice. Nobody
merges.

## Test 3: Stage 2 on a fresh PR (attempt 1/3, nothing reset)

Proves that a Routine corrects the synthetic bug, pushes to the PR's own branch, and that fresh CI and Codex review follow. Real handoff stays off.

* **Use a PR made after the marker fix.** `make-synthetic-branch.sh` now also writes `synthetic/DISPOSABLE_E2E_FIXTURE`; the repository's own
  inert-fixture test tolerates the fixture only with that marker **and** a real branch named `claude/e2e-synthetic-*` (the PR head branch in
  Actions, else the checked-out branch), because the marker travels with the fixture and proves nothing on its own. If a disposable PR were merged
  by mistake, the next suite run on `main`, or on any ordinary PR, goes red. Branches `a` to `e` predate it: on them even the correct fix leaves the
  suite red (that one test), so Stage 2 could never reach green. Never use them.
* **One active Routine per label.** Pause the Stage 1 Routine and create a separate Stage 2 Routine with the same trigger and the Stage 2 prompt
  (or swap the instructions). Two active Routines on `bridge:needs-fix` would both start on one label.
* **Clock.** Create the PR, wait for the failed `unit-tests`, the Codex finding and the gate's `in_flight` attempt 1, check at least 60 minutes of
  lease remain, then add the label at once.
* **Expect:** one session; one commit titled `Address review findings (bridge attempt 1/3)` directly on the head, changing only
  `synthetic/pricing.py`; one push to `claude/e2e-synthetic-<x>`; fresh `unit-tests` (green) and a Codex rereview of the new head; the gate marks
  attempt 1 `superseded` and decides `WAIT` until Codex finishes, then `READY`; one `Bridge [pushed]` comment. Nothing merges.
* **Stop at once** on: a push to any other branch, a force-push, a change under `.github/**`, `bridge-gate/**` or `.claude/**`, more than one commit,
  a merge or approval, a second session on the attempt, or any credit spend. Afterwards switch back to the Stage 1 Routine, close the PR unmerged and
  delete its branch.

## 11. Outcomes

| What you see | Result |
| --- | --- |
| `READY` for the fixed head, one fix commit, $0 credits | **PASS**: the loop works end to end |
| Codex flags a new real problem, the gate hands off again | Fine, up to attempt 3/3; then `MAX_ATTEMPTS` and a notice: **safe stop, PASS for the stop** |
| Session rejected, silent or out of usage, nothing pushed | Attempt times out (`HANDOFF_TIMEOUT`) after the lease: **safe stop** |
| No session starts from the bot's label, or it starts on the wrong commit | **BLOCKER found**; record exactly what happened |
| Any credit spend, extra push, merge, wrong branch | **FAIL**: kill switch, report |

## 12. Teardown

Close C (A and B were closed in step 7) unmerged and delete the `claude/e2e-synthetic-*` branches. Set `BRIDGE_HANDOFF_MODE` to anything but `label`
(or delete it) and set `real_handoff_enabled` back to `false` by a merged PR. Switch the Routine off. Keep the findings
in `SECURITY.md`.
