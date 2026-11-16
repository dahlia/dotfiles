# Call tree, native receipts and attribution

Every CLI invocation propagates `CROSS_SUBAGENTS_RUN_ID`,
`CROSS_SUBAGENTS_PARENT_INSTANCE`, `CROSS_SUBAGENTS_PARENT_TASK_ID`, so recursive
calls belong to the same tree. Native calls must be registered:

```bash
python3 <skill-dir>/scripts/run.py record-native \
  --run-id <run-id> --task-id task-host-native \
  --agent Antigravity --model gemini-2.5-pro \
  --summary "Generated frontend components natively." \
  --outcome-summary "Produced components with tests."
```

This receipt records attribution/text, not independent completion, model,
permissions or protected-input verification. Keep the actual host task ID,
terminal completion event and observed-model evidence separately. Native
Codex uses `codex/openai/<model>`; reports accept older `codex/codex/<model>`
receipts as equivalent for handover completion. For native guarded reviewers
follow the installed code-review-loop skill's two-phase native-begin/finish
recipe. Do not invent CLI cleanup evidence for native execution.

## Contribution judgment and normalization

Successful does not mean significant. Evaluate findings before crediting:

```bash
python3 <skill-dir>/scripts/run.py mark \
  --run-id <run-id> --task-id <task-id> --insignificant \
  --outcome-summary "Reviewed diff; reported no actionable issues."
```

Use official agent names: `Codex`, `Claude Code`, `OpenCode`, `Antigravity`.
Canonical official model IDs are lowercase letters/digits/hyphens/dots.
Scripts strip provider prefixes, context/effort/release-date suffixes and
resolve aliases; they reject nonexistent bare generation prefixes.
Examples: `opencode-go/glm-5.3` → `glm-5.3`,
`claude-sonnet-5[1m]` → `claude-sonnet-5`,
`claude-opus-5-5-low` → `claude-opus-5-5`,
`deepseek-v4.1-flash` → `deepseek-flash`,
`fable` → `claude-fable-5-1`; bare `gpt-6` is invalid.
Respect the repository's crediting rule/trailer syntax without changing IDs.

## Final reporting

```bash
python3 <skill-dir>/scripts/run.py report --run-id <run-id> --include-defects --human
python3 <skill-dir>/scripts/run.py report --run-id <run-id> --only-significant
```

Present the full tree with agents/models, tasks and outcomes unless the user
explicitly omitted it. `--only-significant` filters credit participants, not
handover evidence. Always include automatic/user/timeout quota replacements:
old/new route, reason, decision, changed requirements and completion outcome.
For separate-task/native replacements use `record-handover` in
[quota-handover.md](quota-handover.md).
Name this run's defect paths, severity and title; do not omit existing notices.
`--include-defects` combines the run-filtered defect list with the report;
omitting it preserves the original report contract. The standalone
`defects.py list --run-id <run-id> --human` remains available for diagnostics.
