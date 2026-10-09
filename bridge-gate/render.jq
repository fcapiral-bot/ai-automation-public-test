# Comment bodies for the gate, built ONLY from decision codes, ids and counts (never GitHub text).
# jq -r --arg kind state|notify --slurpfile cfg config.json -f render.jq decision.json
$cfg[0] as $c
| (if ($c.notify_mention | length) > 0 then "@" + $c.notify_mention + " " else "" end) as $who
| def title: "**Bridge gate** · simulated handoff unless `BRIDGE_HANDOFF_MODE=label` · nothing here merges or deploys";
  def msg:
    if .decision == "READY" then
      "READY for human review on `" + .head[0:7] + "`: the current Codex review is clean and independent CI passes. Review and merge it yourself; the gate never merges."
    elif .decision == "MAX_ATTEMPTS" then
      "MAX_ATTEMPTS: " + ($c.max_attempts | tostring) + " correction attempts were used and `" + .head[0:7] + "` still has findings or failing CI. A person must take over."
    elif .decision == "USAGE_STOP" then
      "USAGE_STOP (" + .reason + "): automatic correction is stopped. Remove the kill switch or the `" + $c.labels.usage_stop + "` label to allow it again."
    else
      "BLOCKED (" + .reason + ") on `" + .head[0:7] + "`: automatic correction is paused until a person looks."
    end;
  if $kind == "state" then
    "<!-- bridge-gate-state:v1 " + (.state | tojson) + " -->\n" + title + "\n\n| | |\n| - | - |\n"
    + "| Decision | `" + .decision + "` |\n| Reason | `" + .reason + "` |\n| Head | `" + .head[0:7] + "` |\n"
    + "| Attempts used | " + (.state.attempts | length | tostring) + " of " + ($c.max_attempts | tostring) + " |"
  else
    $who + msg
  end
