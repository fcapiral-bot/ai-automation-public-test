# bridge-gate security review (pre-activation)

Scope: the prototype migrated from the private test repository (its PR #2) (`bridge-gate.yml`, `collect.sh`, `guard.sh`, `gate.jq`, `render.jq`). Method: code
reading plus attacks run against the real scripts and the workflow's real shell steps (fake `gh`), and read-only
checks of the live repository. Nothing was dispatched, enabled or merged.

## Verdict

* **Dry-run activation (simulated handoff only): PASS**, under the conditions below.
* **Real label handoff / Claude Routine: BLOCKED** until the remaining S4 gaps and S5 are resolved (settings issues,
  not code defects), plus the unknown Claude extra-usage state.

Dry-run conditions: the migration PR stays a draft and unmerged; `BRIDGE_HANDOFF_MODE` and `BRIDGE_KILL_SWITCH` stay unset;
Settings → Actions → General has "Allow GitHub Actions to create and approve pull requests" **off** (not
verifiable from here: the API path is blocked for this session).

## The nine checks

| # | Question | Result |
| - | --- | --- |
| 1 | Untrusted PR code with write permissions? | **Fork PRs: no.** `pull_request` and `pull_request_review` from forks get a read-only token and no secrets, and the gate refuses forks (`FORK_PR`). **Same-repo branches: yes, by GitHub design.** Those events take the workflow file and scripts from the PR's merge ref, so anyone who can push a branch can change them and use the token. That is write access they already hold (see S4). `issue_comment` runs only from the default branch. |
| 2 | Trusted scripts from the base branch? | **No** for `pull_request` and `pull_request_review` (PR merge ref). **Yes** for `issue_comment` (default branch). The workflow file itself cannot be pinned to the base for those events. Pin the checkout to the default branch after merge to remove script tampering; the YAML stays PR-controlled (S4). |
| 3 | `pull_request_target` or other privileged event handling? | No `pull_request_target`, schedule or push triggers (pinned by test). Two triggers are privileged by nature because GitHub runs them from the default branch with a write-capable token: `issue_comment` (acts only for the verified Codex bot) and `workflow_run` on completion of the `ci` workflow (acts only for a same-repository pull_request run linked to a PR). Neither checks out or executes PR code, both only read data through the API, and the CI-completion path takes only numeric ids and the PR number from the event and judges the PR's current head from the API. Pinned by `tests/test_bridge_gate_ci_wakeup.py`. |
| 4 | Forged comments, duplicate events, stale commits trigger a handoff? | **Forged comments: no** (identity = login + numeric id + type; a look-alike is a human; forged state and summary comments are ignored). **Duplicates: no** (one attempt per PR+head, lease, write-ahead state). **Stale commits: yes before this review (S1), fixed.** |
| 5 | Attempt state forged, overwritten, reset? | **Forged by a non-bot: no.** **Overwritten: no** (runs are serialised per PR). **Reset: yes by deleting the bot's comment, which any repo writer can do (S3, by design, documented).** Corrupt, duplicate or wrong-version state blocks; it never resets. |
| 6 | Concurrency allows duplicate Claude sessions? | Actions concurrency prevents duplicate **gate runs**. Duplicate **sessions** were possible after a push that was not the session's own (S2), fixed. Residual: bounded by 3 attempts per PR. |
| 7 | Real label handoff disabled by default? | **Yes.** The label step runs only when `vars.BRIDGE_HANDOFF_MODE == 'label'` (unset = empty string = false), is the only label writer, and sits behind the stale-head guard. Pinned by static and executed tests. No routine, label or variable exists. |
| 8 | External contributor can trigger privileged activity or exhaust Actions usage? | **This repo is public**, so anyone can fork and open a PR. Forks get a read-only token and no secrets, and the gate refuses them (`FORK_PR`); GitHub's setting for approving first-time contributors' workflow runs should stay on (not verifiable from here). Cost: each run is under a minute with a 5-minute timeout; `issue_comment` runs for unrelated comments skip at the job level and use no runner minutes; the account budget is $0 with stop-usage. A collaborator can still create many runs (S6). |
| 9 | Safe to activate without invoking Claude or Codex? | **Yes (dry-run).** Nothing in the workflow starts or calls either. Its only writes are the state comment and notification comments on the PR that raised the event. |

## Findings

| ID | Severity | Finding | Status |
| - | - | --- | --- |
| S1 | Medium | **Stale-head handoff.** The head could move between evaluation and the handoff, so a label (and a Claude session) could target a commit already gone. Reproduced: label issued for `0000001` after the head moved to `0000002`. | **Fixed.** `guard.sh` re-reads the head before the state write and again before the label, and refuses if it moved or cannot be read. Regression tests, mutation-checked. |
| S2 | Medium | **Overlapping sessions.** Any new head ended the in-flight attempt, so after a person's push or a rebase a second session could start beside a still-running first one. Reproduced in the simulator. | **Fixed.** An attempt now ends only when the head is one commit directly on the attempt's head titled exactly `Address review findings (bridge attempt N/3)`. Anything else keeps it in flight until the lease expires, then `BLOCKED / HANDOFF_TIMEOUT` and the label is released. |
| S3 | Medium | **State reset by deletion.** Deleting the bot's state comment resets a PR's attempt counter. Needs repo write access. | Open, by design. Hardening idea: floor the count with the number of `Address review findings (bridge attempt …)` commits in the PR. |
| S4 | **Medium for real activation** (was High in the private repo) | **Server-side guardrails are now partly in place.** The ruleset "Protect main – Human Approval" (active, no bypass, 1 required approval, no direct pushes, force-pushes or deletion) is verified: a direct push to `main` was rejected by GitHub (GH013). **Remaining gaps:** no path protection for `.github/**` and `bridge-gate/**`, no required status checks, no code-owner review, and the ruleset itself is editable by a repo admin. A same-repo PR branch can still change the workflow YAML and run it with the token (comments and labels, and pushes to non-`main` branches), but it cannot reach `main` without a human approval. A Routine acting as an admin identity is also stopped from pushing to `main`. | **Partly mitigated.** Add path protection and required checks before real activation. |
| S5 | Medium | **Approval setting unverified.** The gate token has `pull-requests: write`; if "Allow GitHub Actions to create and approve pull requests" is on, a modified workflow could approve a PR. The gate itself never approves. | Open. Check the setting; keep it off. |
| S6 | Low | **Gate runs can be displaced.** A pending gate run for a PR can be replaced by a later run in the same concurrency group, including one started by an unrelated comment that then skips. The event is lost until the next one; the failure direction is safe (no handoff). From GitHub's documented semantics, not reproducible offline. | Open. Give ignored comment events their own group when activating. |
| S7 | Low | **7-character commit match.** Codex's summary row carries a 7-character commit prefix, so a writer could grind a head whose prefix matches an old clean row and get `READY` (advisory; a person still approves and merges). | Open, accepted. |
| S9 | Medium | **Real handoff gated only by a repository variable.** `BRIDGE_HANDOFF_MODE=label` alone would have enabled the label step, and a variable can be set by mistake or by anyone with admin. | **Fixed.** The real step also requires `real_handoff_enabled: true` in `bridge-gate/config.json` **as read from the default branch over the API** (default `false`; missing, unreadable or malformed = locked), so enabling it takes a merged code change plus the variable, and a PR branch editing its own copy of the file cannot open it. Pinned by tests. Residual: a same-repo branch that also edits the workflow YAML can still remove the check (S4). |
| S8 | Info | `actions/checkout@v4` is tag-pinned (GitHub-owned); `GH_TOKEN` is visible to every step (all steps are repo code). | Accepted. |

Checked and clean: no `${{ }}` in any script; no `GITHUB_ENV`/`GITHUB_OUTPUT`/`set-output`; hostile event values
(`$(...)`, backticks, `;`, quotes) in review id, head sha, comment id/time and PR number executed nothing; attacker text
in comments, reviews and check names reached no log, comment or summary; `gh` is only called with `GET` in the
collector, and the only writes are the two PR-comment endpoints and, when enabled, the one label.

## Handoff readiness check (integration prototype)

| Condition | Result |
| --- | --- |
| Automation cannot modify `main` or a production app | **Actions side: established.** `contents: read`; the only writes are PR comments and (locked) one label; pinned by tests. **Routine side: BLOCKED**, because only the Routine's prompt keeps it off `main` (S4: no rulesets on this plan). |
| No real AI execution | **Established.** No workflow references Claude, a Routine, an API key or a token; no Routine exists; the label step is locked; Codex is only an identity filter. |
| No duplicate handoff for one attempt | **Established in the gate** (one attempt per PR + head, write-ahead state, lease, per-PR concurrency). Residual S6. Whether a bot-applied label fires a Routine once is **untested**. |
| Stale commits cannot trigger corrections | **Established in the gate** (guard before the state write and before the label) and **in the prompt** (re-fetch before pushing; prompt-level only). |
| Three attempts maximum | **Established in `gate.jq`.** Open: S3, a repo writer can reset the counter by deleting the state comment. |
| Any error stops safely | **Established for the gate** (collector, guard and state-write failures end with no label). Routine failures end in `BLOCKED / HANDOFF_TIMEOUT` after the lease. |
| Variables alone cannot activate it | **Established.** The lock is read from the **default branch** over the API; missing, unreadable, malformed or non-boolean-true = locked. Pinned by tests. |
| Untrusted PR branches cannot run with privileged permissions | **Forks: established** (read-only token, `FORK_PR`). **Same-repo branch writers: not fully established.** They control the workflow YAML for `pull_request` events (S4), but `main` is protected by the ruleset, so they cannot merge or push there without a human approval. Editing `config.json` on a branch no longer opens the lock. |

## Before the real handoff is enabled

Close the remaining S4 gaps (path protection, required checks) and S5 first, then follow `ROUTINE.md`. Re-check S6. Consider S3's hardening.
