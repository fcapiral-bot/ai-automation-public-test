# TEST FIXTURE: synthetic facts

`needs-fix.facts.json` is **made-up** input for `bridge-gate-fixture.yml`, a dry run that shows a `NEEDS_FIX`
decision, attempt 1 of 3, and a simulated handoff with no AI and no GitHub writes.

* It is **not** a GitHub object and does not impersonate Codex: it is a JSON file with the shape `collect.sh`
  would produce for a PR whose current head carries one Codex finding.
* `gate.jq` **rejects** any facts containing `_fixture` (`BLOCKED / fixture_rejected`) unless the caller passes
  the exact token `--arg allow_fixture test-fixture-run`. Only `bridge-gate-fixture.yml` does; no production file does,
  and `collect.sh` never emits the key. Tests pin all three.
* The fixture workflow's first step is a **control**: normal handling (no token) must reject the fixture, or the
  job fails.
* It runs on a push to the single branch `test/bridge-gate-fixture` (and by manual dispatch once the file is on the
  default branch). Its token is `contents: read`; it contains no label code, no `gh` call, no variable, no secret.
