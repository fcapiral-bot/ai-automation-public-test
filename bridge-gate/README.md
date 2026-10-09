# bridge-gate

A small, native-GitHub gate for the loop **Codex reviews → findings → Claude fixes → Codex re-reviews**,
stopping after three attempts. This directory is a **prototype with a simulated handoff**: it never starts
Claude, never calls Codex, holds no secrets other than the automatic `GITHUB_TOKEN`, and cannot merge,
deploy, push, approve or request reviews.

It is deliberately not a coordinator. One workflow re-reads GitHub on every event, one pure `jq` program
decides, a small guard re-checks the head, and one PR comment holds the state.

| File | Role |
| --- | --- |
| `../.github/workflows/bridge-gate.yml` | Event glue: collect → decide → persist state → notify → handoff (simulated) |
| `collect.sh` | Read-only `gh api` GETs → one normalized facts JSON. Any failure becomes `incomplete` evidence |
| `guard.sh` | Refuses a handoff if the PR head moved since the decision (fail closed) |
| `SECURITY.md` | Pre-activation security review: findings, verdict, what blocks real activation |
| `gate.jq` | **All decisions.** Pure: facts + `config.json` in, decision + next state out. No clock, no network |
| `render.jq` | Comment text, built only from decision codes, ids and counts (never from GitHub text) |
| `config.json` | Max attempts (3), lease (90 min), `require_ci`, the designated CI check (`ci_check`: name `unit-tests` + GitHub Actions app id 15368), the verified Codex identity, label names |
| `ROUTINE.md`, `routine-prompt.md` | Exact steps and prompt to enable the real Claude Routine later. **Not active** |

## Decisions

| Decision | Meaning | Handoff | Notified |
| --- | --- | --- | --- |
| `WAIT` | Nothing to do yet (draft, awaiting Codex, CI pending, fix in flight, …) | no | no |
| `NEEDS_FIX` | Current-head Codex findings or failing CI, attempts left, nothing in flight | **yes (one)** | no |
| `READY` | Current-head Codex review is clean **and** the designated `unit-tests` check (from the GitHub Actions app) succeeded. No other check and no legacy commit status can stand in for it; a failing or pending check of any kind still blocks. Advisory: a person merges | no | yes |
| `BLOCKED` | Anything doubtful: API error, malformed evidence, fork/closed PR, human "changes requested", unclassified Codex review, handoff timeout, or `HANDOFF_LOCKED` (real mode on but the default-branch lock is not confirmed open: no attempt is recorded) | no | yes (not for evidence errors) |
| `MAX_ATTEMPTS` | Three attempts used and the current head still has findings or failing CI | no | yes |
| `USAGE_STOP` | Repo variable `BRIDGE_KILL_SWITCH=true` or PR label `bridge:usage-stop` | no | yes |

Order of evaluation: evidence problems → closed/fork → usage stop → draft → any in-flight attempt
→ human blocker → unclassified Codex review → findings/CI failing (attempt gate) → CI pending → Codex review
present and `Completed` for this head → CI present → `READY`.

## Evidence rules

* **Reviewer identity**: login + numeric id + type must all match the verified Codex App
  (`chatgpt-codex-connector[bot]`, id 199175422). A look-alike is treated as a human.
* **Current head only**: Codex findings count only from reviews whose `commit_id` is the PR's current head.
  A review of an older commit is ignored (reported as `codex_stale`), so a late event can never revive old findings.
* **Findings** = root inline comments of a Codex review on the head (or a Codex `CHANGES_REQUESTED`). A Codex
  review on the head with no root inline comment is `CODEX_REVIEW_UNCLASSIFIED` → `BLOCKED`. Review prose is
  never interpreted. **Absence of findings is never a pass**: `READY` needs a Codex summary row for the head that
  says `Completed`, plus a genuine CI success.
* **CI** = check runs and the combined commit status on the head, excluding this workflow's own check suites,
  identified by numeric `workflow_id` (never by display name). Skipped/neutral alone is not a pass. Unknown
  conclusions block.
* Any missing, malformed or contradictory evidence (including a status for another commit or a truncated status
  list) → `BLOCKED / EVIDENCE_UNAVAILABLE`, with **no state write, no handoff, no notification**.

## State, attempts and duplicate prevention

State is one PR comment, authored by `github-actions[bot]`, whose first line is
`<!-- bridge-gate-state:v1 {json} -->`. A comment with that marker from anyone else is ignored; two genuine
ones, or a corrupt one, block (a corrupt state never resets the attempt counter).

```json
{"v":1,"attempts":[{"n":1,"head":"<40-hex>","trigger":"review:123","reason":"CODEX_FINDINGS",
  "at":1800000000,"lease_expires":1800005400,"status":"in_flight"}],
 "seen":["review:123"],"last":{"decision":"NEEDS_FIX","reason":"CODEX_FINDINGS","head":"<40-hex>"}}
```

Four independent layers keep two correction sessions from overlapping:

1. **Serialised runs**: `concurrency: group: bridge-gate-<PR>` with `cancel-in-progress: false`. GitHub runs one
   gate per PR at a time and keeps only the newest pending one; each run re-reads GitHub, so a dropped
   intermediate run loses nothing.
2. **One attempt per (PR, head)**: the attempt record is keyed by head SHA. While it is `in_flight` and its lease
   is unexpired the decision is `WAIT / HANDOFF_IN_FLIGHT` whatever event arrives.
3. **Write-ahead state**: the state comment is written *before* the handoff step, and the job stops at the first
   failing step. If the write fails there is no handoff.
4. **Only the session's own commit completes an attempt**: the head must be one commit directly on the attempt's
   head, titled exactly `Address review findings (bridge attempt N/3)`. Any other new head (a person's push, a
   rebase, a merge) leaves the attempt in flight until its lease expires, then `BLOCKED / HANDOFF_TIMEOUT`
   (and the label is released). The gate never starts a second session beside a possibly still-running one.

A separate **stale-head guard** (`guard.sh`) re-reads the PR head right after the decision (before any write) and
again immediately before the real label, and refuses the handoff if the head moved or cannot be read.

Attempts are counted from this persisted state (all statuses count, including timed out), not from commit
counts. `seen` keeps the last 20 event keys; a repeated key sets `duplicate: true` for logging. Duplicates are
harmless by construction (decisions are level-triggered), so the flag is informational. To reset a PR's
counter on purpose, a person deletes the bot's state comment.

## Simulated handoff

`NEEDS_FIX` logs `SIMULATED HANDOFF: would add label "bridge:needs-fix" to PR #N for head <sha7> (attempt k/3)`.
The real step (adds that label; removes it when a new head supersedes the attempt) exists in the workflow but
runs only when the repository variable `BRIDGE_HANDOFF_MODE` is exactly `label` **and** the lock
`real_handoff_enabled` in `config.json` is `true` **on the default branch** (read over the API, never from the PR
checkout; it is `false`, and an unreadable file counts as locked). See `ROUTINE.md`.

## Fixture dry run

`.github/workflows/bridge-gate-fixture.yml` runs the real `gate.jq` on **synthetic** facts (`fixtures/needs-fix.facts.json`)
to show `NEEDS_FIX`, attempt 1 of 3, one simulated handoff and no duplicate, with a read-only token and no GitHub or AI
calls. See `fixtures/README.md`.

## Run it locally

```
python3 -m tests.gate_sim                              # event → decision → handoff demo, no network
python3 -W error -m unittest discover -s tests -t .    # whole suite (needs jq)
```

## Known limitations

* **Resolved review threads** are not visible through REST. Findings are judged per head, so a fix pushed as a
  new commit clears them; manually resolving a thread on the same head does not.
* **CI completion wakes the gate through `workflow_run`** (workflow named `ci`, type `completed`), so a gate run that
  saw CI pending is re-evaluated when CI finishes. GitHub runs `workflow_run` only from the **default branch's** copy of
  `bridge-gate.yml`, so it is inert until this workflow is merged to `main`; it cannot be exercised from the PR branch.
  Only GitHub Actions CI is covered (`check_run`/`check_suite` events are not delivered for checks created by Actions,
  and a plain commit status from another service raises no event the gate listens to). The event supplies only a numeric
  run id and the PR number; the gate always judges the PR's current head from the API. If CI is still reported as
  running at that moment, the manual `workflow_dispatch` (`pr_number`) re-evaluates once.
* **`issue_comment` runs only from the default branch** and only for comments by the Codex App. Until this
  workflow is on `main`, clean-review detection (the Codex summary comment) is inert.
* **Codex "clean" detection** relies on the summary comment layout observed on a real Codex review
  (`<!-- codex-pull-request-review-summary -->`, a row with the 7-character commit and `**Completed**`). A
  rewording makes the gate wait, never pass.
* The gate does **not** request Codex reviews. Whether Codex re-reviews new commits on its own is unverified.
* The workflow checks out the PR's merge ref to get these scripts. That is acceptable for same-repository
  branches only; forks are refused (`FORK_PR`). After merging to `main`, pin the checkout to the default branch.

## Status

Prototype. Simulated handoff only. Migrated from the private test repository (source commit `11c8fbb` of its
`proto/bridge-gate` branch) into this public test repository. Nothing here starts Claude or Codex, and the real
handoff stays locked (`real_handoff_enabled: false`, no `BRIDGE_HANDOFF_MODE`).

Merge protection: `main` requires a pull request, a passing `unit-tests` check and resolved review conversations. It does **not**
require an approving review (required approvals = 0, a deliberate owner exception for this one-person test repository), and zero
approvals does not prevent an authorized repository writer from merging. That is an unresolved security blocker for unattended AI
execution; see `SECURITY.md` S10.

Migration note: the gate listens to `pull_request` `synchronize`, `reopened` and `ready_for_review`, not `opened`, so a
brand-new PR is first evaluated when its next commit is pushed. This documentation-only commit is that first push.
