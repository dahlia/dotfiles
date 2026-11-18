# Sessions and coroutine resumption

Read when preserving or resuming child conversations.

Default one-shot runs suppress/delete interactive session records where
supported: Codex `--ephemeral`, Claude `--no-session-persistence`, OpenCode
verified `session delete --standalone`. Antigravity v0.2.2 persists state in
`~/.gemini/antigravity-cli` and has no delete command; cleanup is reported false.

1. Initial invocation: pass `--keep-session`. The recorded session state
   (`session_id`, `route_id`, `effort`, `sandbox_mode`, `cwd`, `resumable`)
   lives in `$SKILL_CACHE_DIR/runs/<run-id>/sessions.json`.
2. Follow-up: use a new task ID and prompt, resuming the initial task:

```bash
python3 <skill-dir>/scripts/run.py \
  --run-id <run-id> --task-id <followup-task-id> \
  --resume-task <initial-task-id> \
  --context-file <external-context.md> --add-dir <external-directory> \
  --allow-shell --isolate \
  --prompt-file <followup-prompt.md>
```

Repeat `--context-file`, `--add-dir`, `--allow-shell`, `--no-tools`, `--isolate`
and `--guard` when needed: these grants/policies are **not restored by resume**.
The example's flags are optional; do not add permissions not needed initially.
Route, effort, sandbox mode and cwd are inherited unless supported overrides
are supplied.
`--resume-task` forces session retention internally, even when `--keep-session`
is omitted on the follow-up; omission does not make a resumed turn ephemeral.

After a timeout, `resumable: true` means a session ID was retained, subprocess
cleanup was verified, and there was no known model mismatch or unsupported
resume path. It does not mean the task finished. If protected inputs are
unchanged (including any external guard), resume the **timed-out task ID**
with a new task ID and a short continuation prompt. Keep the same route and
permissions. If the CLI cannot load that session, report the failed resume
and choose a fresh route only under the original constraints.

Resume stored conversation state when supported. Retention, compaction and
accounting depend on CLI/model. Summarize and start fresh when changing model
families or when prior history is no longer useful.
