# Enabling the real Claude Routine handoff (NOT ACTIVE)

Today the gate only **logs** a handoff. Nothing below has been done: no routine exists, no label exists,
`BRIDGE_HANDOFF_MODE` is unset, and no workflow starts Claude. This page is the exact sequence for turning
it on later, in order. Stop at the first step you cannot confirm.

How the real handoff works: when `gate.jq` decides `NEEDS_FIX` and has recorded the attempt, the workflow adds
the label `bridge:needs-fix` to the PR. A Claude Code cloud **Routine** with a GitHub trigger
(Pull request → `labeled`, filtered to that label) starts one cloud session on your Team seat, which makes
one fix commit on the PR's head branch. That commit, titled exactly `Address review findings (bridge attempt N/3)`
and sitting directly on the attempt's head, is what tells the gate the attempt is finished; any other push does not.

## 0. Preconditions (none are confirmed today)

1. **Claude usage credits are OFF** for the whole organization, with no group or member override
   (admin console → Usage). This is the only provider-side lever that stops a routine at the seat allowance:
   the routines docs say that without usage credits "additional runs are rejected until your usage window
   resets". Also keep managed **Code Review** off (it is billed only through usage credits).
2. **Routines and cloud sessions are allowed** by an organization Owner, and the **Claude GitHub App is
   installed on `fcapiral-bot/ai-automation-public-test`** (GitHub triggers need it).
3. **Codex is enabled for this repository** with purchased credits disabled, and you know how a new commit
   gets re-reviewed (see section 6). Automatic review and re-review on every push were verified in the earlier private test
repository; they have not been tested here, and Codex is not confirmed to be enabled for this repository.
4. **A branch ruleset on `main` that GitHub enforces for a human decision.** Intended: an approving review from a human
   other than the author, no bypass for the account the routine acts as, protection of `.github/**` and `bridge-gate/**`
   (rule or CODEOWNERS), and auto-merge off, so neither the routine nor an Action can rewrite the gate or merge.
   "Allow GitHub Actions to create and approve pull requests" stays off. **Status here (verified 2026-10-09):** the ruleset
   "Protect main – Human Approval" is active with no bypass actors. It requires a pull request, a passing `unit-tests`
   check, resolved review conversations, and forbids force-pushes and deletion; a direct push to `main` was rejected by
   GitHub (GH013, "Changes must be made through a pull request"). **It requires 0 approving reviews.** The owner lowered it
   from 1 on purpose because this disposable repository has one human, and merges the simulated-only prototype personally:
   a conscious exception, **not** GitHub-enforced human approval. Zero approvals does not prevent an authorized repository
   writer from merging. **This is an unresolved security blocker for unattended AI execution (`SECURITY.md` S10):** do not
   create a Routine or unlock the handoff until at least 1 approving review from a non-author human is required (this needs
   a second human account), path protection exists, and the automation's identity can neither merge nor edit the ruleset.
5. You accept that routines belong to **your** claude.ai account: commits and comments appear as you, and
   runs draw on your seat's usage.
6. **A lease-expiry watchdog exists, or you knowingly accept manual-only timeouts for a supervised pilot.** Nothing runs the gate when a
   lease expires (`README.md` Known limitations, `SECURITY.md` S15). Until a bounded scheduled watchdog is built, a stuck attempt is
   noticed only when another event arrives or you run the workflow by hand (`workflow_dispatch`, `pr_number`). **This is a documented
   blocker for unattended activation.** It was deferred on purpose and is not implemented.

## 1. Create the labels (repository write: do it by hand)

`bridge:needs-fix` (the handoff) and `bridge:usage-stop` (a manual stop). The names live in `config.json`.

## 2. Create the routine (claude.ai/code/routines → New routine)

* **Instructions**: the **Stage 1 report-only** prompt in `routine-prompt.md`.
* **Repository**: `fcapiral-bot/ai-automation-public-test` only.
* **Environment**: Default (Trusted network). Add no environment variables or secrets.
* **Connectors**: remove all of them.
* **Trigger**: add exactly one. GitHub event → repository `fcapiral-bot/ai-automation-public-test` → event **Pull request**,
  action **labeled** → filters: *Labels* includes `bridge:needs-fix` and *Is draft* is `false`.
  No schedule trigger and no API trigger (an API trigger needs a bearer token; do not create one).
* GitHub events are subject to per-routine and per-account hourly caps, and events beyond the cap are dropped.
  A dropped event looks like a stuck attempt. It is noticed, as `BLOCKED / HANDOFF_TIMEOUT` and never as a retry, only when the gate is
  next evaluated after the lease: another event, or a manual `workflow_dispatch` (there is no watchdog, see precondition 6).

## 3. Smoke-test the trigger by hand (still no gate handoff)

On a throwaway, non-draft PR with a Codex finding, **add the label yourself**. Expect exactly one routine
session that only posts a report-only comment and pushes nothing. Check at claude.ai/settings/usage (and the
admin spend report) that the run used seat allowance and **$0 of usage credits**. Remove the label.

## 4. Switch the routine to the Stage 2 prompt

Replace the instructions with the **Stage 2 correction** prompt. Repeat step 3 on a throwaway PR and confirm
one commit on the PR's head branch, no force-push, no merge, no `.github/**` change.

## 5. Turn on the gate's handoff

Two independent locks must both be opened, so no single mistake enables it: (1) set `"real_handoff_enabled": true` in
`bridge-gate/config.json` **and merge that change to the default branch** (the workflow reads the lock from the default
branch over the API, never from the PR's own checkout, and treats "missing, unreadable or not exactly true" as locked),
and (2) Settings → Secrets and variables → Actions → **Variables** → `BRIDGE_HANDOFF_MODE` = `label` (a variable, not a
secret). From then on `NEEDS_FIX` adds the label after the state has been written, and a new head removes it. Watch the
first real run end to end on a throwaway PR.

**Unverified, test here first:** whether a label applied by `github-actions[bot]` fires the routine. Events made
with the workflow token do not start *workflows*; routines are fired by the Claude GitHub App's webhook, which
has not been tested with a bot-applied label. If it does not fire, nothing breaks: the attempt stays in flight for the 90-minute lease, and
its timeout (`BLOCKED / HANDOFF_TIMEOUT`, label cleanup, notice) is recorded only when another event arrives or the workflow is
dispatched manually after the lease (no watchdog; precondition 6). Do not "fix" this by creating a token or an API
trigger without a separate decision.

Also unverified: how the routine session is told which PR fired it. The prompt therefore finds the PR by label
and checks it against the state comment, and stops unless exactly one PR matches and the head agrees.

## 6. Codex re-review (the remaining blocker for an unattended loop)

The gate never asks for a review. After Claude pushes, the state is `WAIT / AWAITING_CODEX_REVIEW` until Codex
reviews the new head. Codex's documented triggers are PR opened, draft marked ready, and a comment
`@codex review`; automatic re-review of new commits is not documented. Before relying on the loop, test on a
throwaway PR (each case uses one review from your plan allowance): a plain push, a human `@codex review`, an
`@codex review` posted by a bot, and a draft → ready toggle by a bot. If none works unattended, the loop pauses
for a human `@codex review` after each fix.

## 7. Stop, pause, reset

| To … | Do |
| --- | --- |
| Stop all automatic correction now | Set repository variable `BRIDGE_KILL_SWITCH` = `true` → every PR decides `USAGE_STOP` |
| Stop one PR | Add the label `bridge:usage-stop` |
| Go back to log-only | Set `BRIDGE_HANDOFF_MODE` to anything but `label`, or delete it |
| Pause the routine itself | Switch it off at claude.ai/code/routines |
| Evaluate a stuck or expired attempt now | Run the workflow by hand: Actions → bridge-gate → Run workflow, with `pr_number` (`workflow_dispatch`). Nothing else wakes the gate when a lease expires |
| Retry a head after a timeout, or reset a PR's attempts | Delete the bot's state comment on that PR (or push a new commit) |
| Resume after usage resets | Clear the kill switch / label by hand. There is no automatic resume: nothing can confirm from a workflow that usage credits are still off |

## 8. Usage exhaustion

When the seat is out of allowance and credits are off, the routine run is rejected and nothing is pushed. The
gate sees no new head and nothing wakes it when the lease ends; the next evaluation after the 90-minute lease (another event, or a
manual `workflow_dispatch`) blocks and notifies. Set `bridge:usage-stop` (or the
kill switch) until the usage window resets, then clear it by hand. Do not turn usage credits on to "get
through": that is exactly the spend this design is built to avoid.
