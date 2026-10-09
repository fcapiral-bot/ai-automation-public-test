"""Test support and demo for bridge-gate: fact builders, a jq runner, REST fixtures and an event simulator.

Not a coordinator: gate.jq makes every decision. This module only feeds it facts, carries the
persisted state between events the way the workflow's state comment does, and logs the handoffs.
Run `python3 -m tests.gate_sim` to watch a simulated event -> decision -> handoff sequence.
"""

import copy
import json
import os
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
GATE = ROOT / "bridge-gate"
CONFIG = json.loads((GATE / "config.json").read_text())
CODEX = {"login": CONFIG["codex"]["login"], "uid": CONFIG["codex"]["id"], "type": CONFIG["codex"]["type"]}
HUMAN = {"login": "octocat", "uid": 583231, "type": "User"}
LOOKALIKE = {"login": CODEX["login"], "uid": 999, "type": "Bot"}  # right name, wrong id
NOW = 1_800_000_000
GITHUB_ACTIONS_APP_ID = 15368   # the app that produces the designated check (and the ruleset's integration_id)


def sha(n):
    """A distinct, readable 40-hex head: sha(2) starts with 0000002."""
    return (("%07x" % n) * 6)[:40]


def run_gate(facts, config=None):
    """Run the real gate.jq on `facts` and return its decision object."""
    cfg = ROOT / "bridge-gate" / "config.json"
    with tempfile.TemporaryDirectory() as tmp:
        if config is not None:
            cfg = pathlib.Path(tmp) / "config.json"
            cfg.write_text(json.dumps(config))
        proc = subprocess.run(
            ["jq", "-n", "--slurpfile", "facts", "/dev/stdin", "--slurpfile", "cfg", str(cfg), "-f", str(GATE / "gate.jq")],
            input=json.dumps(facts), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def facts(head=None, **over):
    """The quiet baseline: open, non-draft PR, no reviews, no CI, no state."""
    head = head or sha(1)
    base = {
        "now": NOW, "event": {"name": "pull_request_review", "key": None}, "incomplete": [], "kill_switch": False,
        "handoff_mode": "simulate",
        "pr": {"number": 7, "state": "open", "merged": False, "draft": False, "head_sha": head,
               "head_repo": "o/r", "base_repo": "o/r", "labels": []},
        "reviews": [], "review_comments": [], "issue_comments": [], "check_runs": [], "own_suite_ids": [],
        "head_commit": {"subject": "work in progress", "parents": []},
        "combined_status": {"state": "pending", "total": 0, "listed": 0, "sha": head},
        "state": None, "state_comment_id": None,
    }
    base.update(over)
    if "head_sha" in over.get("pr", {}) and "combined_status" not in over:
        base["combined_status"]["sha"] = over["pr"]["head_sha"]
    return base


def review(rid, commit, who=CODEX, state="COMMENTED"):
    return {"id": rid, "login": who["login"], "uid": who["uid"], "type": who["type"], "state": state, "commit_id": commit}


def inline(review_id, reply_to=None):
    return {"review_id": review_id, "in_reply_to": reply_to}


def summary(head, who=CODEX, completed=True, other_commit=None):
    row = "| 📝 **Code Review** | %s | `%s` | PR opened |" % ("✅ **Completed**" if completed else "⏳ **In progress**", head[:7])
    rows = row if other_commit is None else "| 📝 **Code Review** | ✅ **Completed** | `%s` | old |\n%s" % (other_commit[:7], row)
    body = CONFIG["summary_marker"] + "\n\n## Codex Review Summary\n\n| Review | Status | Commit | Trigger |\n| - | - | - | - |\n" + rows
    return {"id": 9000, "login": who["login"], "uid": who["uid"], "type": who["type"], "body": body}


def check(name, status="completed", conclusion="success", suite=100, app_id=None):
    """A normalized check run as collect.sh emits it. `app_id` defaults to the designated GitHub Actions app."""
    return {"name": name, "status": status, "conclusion": conclusion, "suite_id": suite,
            "app_id": GITHUB_ACTIONS_APP_ID if app_id is None else app_id}


class Simulator:
    """Feeds events through gate.jq, persisting state exactly like the workflow's state comment."""

    def __init__(self, log=None):
        self.state = None
        self.facts = facts()
        self.handoffs = []
        self.log = log or (lambda line: None)

    def event(self, label, key, mutate=None, **top):
        if mutate:
            mutate(self.facts)
        self.facts["event"] = {"name": label, "key": key}
        self.facts["state"] = copy.deepcopy(self.state)
        self.facts.update(top)
        d = run_gate(self.facts)
        if d["state_changed"]:
            self.state = d["state"]  # write-ahead: state is persisted BEFORE any handoff
        if d["handoff"]:
            self.handoffs.append(d["handoff"])
        flags = (" duplicate" if d["duplicate"] else "") + (" NOTIFY" if d["notify"] else "") + (" clear-label" if d["clear_label"] else "")
        line = "%-34s -> %-12s %-26s%s" % ("%s %s" % (label, key or ""), d["decision"], d["reason"], flags)
        if d["handoff"]:
            h = d["handoff"]
            line += "\n%38s SIMULATED HANDOFF: would add label '%s' to PR #%d for head %s (attempt %d/%d, mode=%s)" % (
                "", h["label"], h["pr"], h["head"][:7], h["attempt"], h["of"], h["mode"])
        self.log(line)
        return d

    @property
    def head(self):
        return self.facts["pr"]["head_sha"]

    def push(self, n, foreign=False):
        """The head moves. By default the correction session pushed its one titled commit on top of the old head;
        foreign=True is anyone else's push (a person, a rebase)."""
        old = self.facts["pr"]["head_sha"]
        attempts = (self.state or {}).get("attempts", [])
        inflight = [a for a in attempts if a["status"] == "in_flight"]
        number = inflight[-1]["n"] if inflight else len(attempts) + 1
        title = "Address review findings (bridge attempt %d/%d)" % (number, CONFIG["max_attempts"])
        self.facts["pr"]["head_sha"] = sha(n)
        self.facts["combined_status"]["sha"] = sha(n)
        self.facts["head_commit"] = {"subject": "human push" if foreign else title, "parents": [old]}


def demo(out=print):
    out("== Scenario 1: findings -> handoff -> duplicates -> new head -> repeat until the limit ==")
    s = Simulator(out)
    s.event("pull_request_review", "review:101", lambda f: f.update(
        reviews=[review(101, sha(1))], review_comments=[inline(101), inline(101)], check_runs=[check("unit-tests")]))
    s.event("pull_request_review", "review:101")  # GitHub re-delivers the same event
    s.event("pull_request_review", "review:102", lambda f: f["reviews"].append(review(102, sha(1), LOOKALIKE)))
    s.push(2)
    s.event("pull_request synchronize", "head:" + sha(2)[:7])
    s.event("pull_request_review (late)", "review:101")  # delayed delivery of the OLD review
    s.event("pull_request_review", "review:103", lambda f: (f["reviews"].append(review(103, sha(2))), f["review_comments"].append(inline(103))))
    s.push(3)
    s.event("pull_request synchronize", "head:" + sha(3)[:7])
    s.event("pull_request_review", "review:104", lambda f: (f["reviews"].append(review(104, sha(3))), f["review_comments"].append(inline(104))))
    s.push(4)
    s.event("pull_request synchronize", "head:" + sha(4)[:7])
    s.event("pull_request_review", "review:105", lambda f: (f["reviews"].append(review(105, sha(4))), f["review_comments"].append(inline(105))))
    out("handoffs issued: %d (limit %d)\n" % (len(s.handoffs), CONFIG["max_attempts"]))

    out("== Scenario 2: clean Codex summary + passing CI -> READY (advisory; nothing merges) ==")
    s = Simulator(out)
    s.event("pull_request_review", "review:201", lambda f: f.update(issue_comments=[summary(f["pr"]["head_sha"])],
                                                                      check_runs=[check("unit-tests")]))
    out("")
    out("== Scenario 3: kill switch, API failure, stuck handoff ==")
    s = Simulator(out)
    s.event("pull_request_review", "review:301", lambda f: f.update(reviews=[review(301, sha(1))], review_comments=[inline(301)],
                                                                      check_runs=[check("unit-tests")]), kill_switch=True)
    s.event("pull_request_review", "review:301", kill_switch=False, incomplete=["reviews"])
    s.event("pull_request_review", "review:301", incomplete=[])
    s.event("workflow_dispatch", None, now=NOW + 91 * 60)
    return s


if __name__ == "__main__":
    demo()
