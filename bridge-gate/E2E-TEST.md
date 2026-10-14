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
the decisions on S10 and S18 (`SECURITY.md`) taken and written down; this change merged by you. Until S10 and S18 are decided
this test must not start: the Routine acts as your admin identity.

## 2. Labels and Routine (Stage 1 prompt)

Create the labels `bridge:needs-fix` and `bridge:usage-stop`. Create the Routine exactly as `ROUTINE.md` section 2 says, with the
**Stage 1 report-only** prompt. `BRIDGE_HANDOFF_MODE` stays unset and `real_handoff_enabled` stays `false`.

## 3. Two throwaway PRs with a synthetic bug

In a clone: `bridge-gate/e2e/make-synthetic-branch.sh a`, `git push -u origin claude/e2e-synthetic-a`, open a **non-draft** PR.
Repeat with `b`. The bug: `apply_discount` returns the discount instead of the discounted price, so `unit-tests` fails.
Expect (simulate mode): `ci` fails; the gate decides `NEEDS_FIX` and its log says `SIMULATED HANDOFF` (attempt 1/3); **no label**,
**no Routine session**. Codex reviews each PR automatically and may also flag the bug (not required: failing CI alone is enough
to start an attempt, and a Codex finding does not change the fix).

## 4. Cost check before any Claude run

Open claude.ai/settings/usage and the admin spend report. Note the current numbers. Credits must be $0 and stay $0.

## 5. Trigger and PR identity, by hand (checks `SECURITY.md` S17)

Add the label `bridge:needs-fix` yourself to **both** PRs. Expect **two** Routine sessions, one per PR (this proves a label event
starts a session at all). Open each session: its first command is `git rev-parse HEAD`, and it must equal the head of its own PR,
not the other's. No attempt is in flight (the gate recorded none), so each session must stop at step 2 and post and push
nothing. Remove both labels. If a session started on `main`, or acted on the wrong PR, stop: the identity rule does not hold.

## 6. Cost check

The two sessions must show seat usage only. Any credit spend: stop.

## 7. Open both locks, for the throwaway PRs only

Merge a PR that sets `real_handoff_enabled` to `true` in `bridge-gate/config.json` **and** set the variable
`BRIDGE_HANDOFF_MODE` = `label`. Then Actions → bridge-gate → Run workflow with `pr_number` = A. Expect `NEEDS_FIX`, state comment
with attempt 1 in flight (lease 90 minutes), then the gate adds the label. Do the same for B.

## 8. Does a bot-applied label fire the Routine? (checks `SECURITY.md` S17, still the Stage 1 prompt)

Expect two sessions, one per PR, each finding its own PR by head, in-flight attempt 1, each posting ONE report-only comment and
pushing nothing. **If no session starts, the label route is unavailable: stop and report.** Do not create a token or an API trigger
(that is a separate decision).

## 9. The watchdog (checks `SECURITY.md` S15)

Stage 1 never finishes an attempt, so both leases expire. Do not touch anything. Within the lease plus about two hours (the
watchdog runs hourly at minute 23), expect for each PR: a `bridge-watchdog` run that dispatched `bridge-gate`, then the state
`BLOCKED / HANDOFF_TIMEOUT`, the label removed, one notice. If that has not happened after 3.5 hours, dispatch the gate by hand
and record the watchdog as **unverified**: the loop does not rely on it for safety, only for liveness.

## 10. The correction loop (Stage 2 prompt)

Close PR B unmerged. Switch the Routine to the **Stage 2 correction** prompt. On PR A delete the bot's state comment (this resets
its attempt count on purpose) and run the gate by hand (`pr_number` = A). Expect, in order: attempt 1/3 recorded; label; one
session; **one commit** titled `Address review findings (bridge attempt 1/3)` on `claude/e2e-synthetic-a` (it makes
`apply_discount` return `price_cents - price_cents * percent // 100`, and changes no workflow, no `bridge-gate/**`); `unit-tests`
passes; Codex reviews the new head by itself (👀, then 👍 or a review); the gate decides `READY` and posts its notice. Nobody
merges.

## 11. Outcomes

| What you see | Result |
| --- | --- |
| `READY` for the fixed head, one fix commit, $0 credits | **PASS**: the loop works end to end |
| Codex flags a new real problem, the gate hands off again | Fine, up to attempt 3/3; then `MAX_ATTEMPTS` and a notice: **safe stop, PASS for the stop** |
| Session rejected, silent or out of usage, nothing pushed | Attempt times out (`HANDOFF_TIMEOUT`) after the lease: **safe stop** |
| No session starts from the bot's label, or it starts on the wrong commit | **BLOCKER found**; record exactly what happened |
| Any credit spend, extra push, merge, wrong branch | **FAIL**: kill switch, report |

## 12. Teardown

Close the test PRs unmerged and delete the `claude/e2e-synthetic-*` branches. Set `BRIDGE_HANDOFF_MODE` to anything but `label`
(or delete it) and set `real_handoff_enabled` back to `false` by a merged PR. Switch the Routine off. Keep the findings
in `SECURITY.md`.
