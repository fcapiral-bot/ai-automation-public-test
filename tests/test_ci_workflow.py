"""ci.yml: static guardrails. The CI workflow runs untrusted pull-request code, so it must stay minimal and unprivileged."""

import json
import pathlib
import re
import unittest

from . import gate_sim as g

CI = g.ROOT / ".github" / "workflows" / "ci.yml"
TEXT = CI.read_text()
CODE = "\n".join(l for l in TEXT.splitlines() if not l.lstrip().startswith("#"))


class CiWorkflowSafetyTests(unittest.TestCase):
    def test_it_runs_only_on_pull_request_never_a_privileged_or_scheduled_event(self):
        on_block = CODE.split("\non:", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual(re.findall(r"^  ([a-z_]+):", on_block, re.M), ["pull_request"])
        self.assertNotRegex(CODE, r"pull_request_target|workflow_run|schedule|issue_comment|workflow_dispatch|\bpush:")

    def test_the_token_is_read_only_at_workflow_and_job_level(self):
        self.assertEqual(re.findall(r"^permissions:\n((?:  .+\n)+)", CODE + "\n", re.M), ["  contents: read\n"])
        self.assertNotRegex(CODE, r"(?m)^\s+[a-z-]+:\s*write\b")
        self.assertNotIn("write-all", CODE)
        self.assertNotRegex(CODE, r"(?m)^    permissions:")  # the job inherits the workflow's read-only token

    def test_no_secrets_no_token_in_the_environment_and_no_credentials_left_behind(self):
        self.assertNotRegex(CODE, r"secrets\.|GITHUB_TOKEN|GH_TOKEN|\benv:")
        self.assertIn("persist-credentials: false", CODE)

    def test_only_github_owned_actions_are_used(self):
        used = re.findall(r"(?m)^\s*(?:-\s+)?uses:\s*(\S+)", CODE)
        self.assertEqual(used, ["actions/checkout@v4"])

    def test_no_event_data_is_interpolated_into_any_script(self):
        for block in re.findall(r"run:\s*\|?\n?((?:\s{10,}.*\n?)+)", CODE):
            self.assertNotIn("${{", block)
        self.assertEqual(re.findall(r"run: (.+)", CODE), ["python3 -W error -m unittest discover -s tests -t ."])

    def test_no_ai_paid_service_or_network_tool_is_referenced(self):
        self.assertNotRegex(CODE, r"(?i)claude|anthropic|codex|openai|api[_-]?key|curl |wget |gh api|pip install|npm ")

    def test_the_job_is_short_and_named_as_the_gate_expects(self):
        self.assertIn("timeout-minutes: 10", CODE)
        self.assertIn("\n  unit-tests:\n", CODE)
        self.assertIn("runs-on: ubuntu-latest", CODE)

    def test_the_workflow_is_small(self):
        self.assertLess(len(TEXT.splitlines()), 40)

    def test_the_gate_still_requires_ci(self):
        # Adding CI must not weaken bridge-gate: require_ci stays on, and failing checks still force a fix.
        cfg = json.loads((g.GATE / "config.json").read_text())
        self.assertIs(cfg["require_ci"], True)
        self.assertIs(cfg["real_handoff_enabled"], False)


if __name__ == "__main__":
    unittest.main()
