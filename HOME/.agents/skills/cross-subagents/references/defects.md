# Skill defect reports

Check open defects at startup and avoid those affecting the plan. Record
defects in this skill immediately, continue using a safe workaround, and tell
the user about this run's reports at completion. Do not stop to fix the skill
unless asked.

Report helper tracebacks, wrong status/error classification, documented flags
rejected by installed CLIs, adapter misparsing, routes incompatible with
requested posture/capabilities, missing reachable models, invented workarounds
for documented behavior, or misclassification causing wasted fallback quota.
Do not report answer-quality complaints, user-code failures or ordinary
quota/timeouts: those belong to execution results.

```bash
python3 <skill-dir>/scripts/defects.py new \
  --title "Codex misclassifies a completed review" \
  --summary "A failing test's output was treated as a tool denial." \
  --severity blocker --area "scripts/run.py _tool_denial" \
  --step "Invoke run.py" \
  --observed "status=failed, error_class=permission" \
  --expected "status=ok with verified final answer" \
  --reproduction "<exact command>" \
  --impact "Discarded completed answers and wasted fallback quota." \
  --evidence "<run-dir>/<task-id>/attempts/attempt-1/events.jsonl" \
  --run-id <run-id> --task-id <task-id>
```

One short defect per file; reference evidence instead of pasting logs.
No tokens, secret-containing prompts or proprietary code in reports.
`new` prints a path; `--json` returns id/path/severity/title.
Reports live in `$SKILL_CACHE_DIR/defects/`, overridden by
`CROSS_SUBAGENTS_DEFECT_DIR`; severities are blocker, major, minor.

```bash
python3 <skill-dir>/scripts/defects.py list --human
python3 <skill-dir>/scripts/defects.py list --run-id <run-id> --human
```

At completion list each created report's path, severity and title once.
Only after an actual fix, run
`defects.py resolve --id <id> --note "<fix>"`; this moves the report into
`defects/resolved/` and appends the note.
