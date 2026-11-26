# Execution artifacts and result diagnosis

Read for artifact layout, incomplete answers, denials or recovered errors.

## Artifacts

Everything is under `$SKILL_CACHE_DIR/runs/<run-id>/`:

- `prompts/common.md`, `prompts/task-<task-id>.md`: reused on fallback.
- `<task-id>/attempts/attempt-N/`: isolated stdout, stderr and event logs.
- `<task-id>/result.md`: verified final text, published only on success.
- `<task-id>/meta.json`: [metadata schema](../assets/schemas/meta.schema.json).
  Important fields include status/exit code/duration, requested/observed model,
  model_verified, session/resumable/cleanup, usage, selection rationale,
  tool_policy/tools_enforced, denials_observed, warnings, error_class, guard,
  and attempt_result_file.
- `manifest.jsonl`: append-only task-start/task-finish audit trail.

Use `run.py inspect --run-id <run-id> --task-id <task-id> --human` for a compact
metadata view with final/partial result paths. The default JSON form is
machine-readable; neither mode echoes the answer or raw events.
It preserves denials, warnings, guard data, maintenance notices and policy/model
evidence without reclassifying status or judging usefulness.
Exit 0 means status ok plus a readable non-empty final answer, not a complete
quality/safety verdict. Review ATTENTION items even with exit 0. Failure,
missing metadata, empty final text or escaped result pointers yield exit 1.
Inspection uses the writer's task lock and fails immediately if a new attempt
is publishing/running; wait for host completion notification, do not poll.
Metadata/results must be regular, task-contained files. CLI success honors its
explicit final pointer; only native receipts lacking that field use the legacy
task-local result path.
Inspect the full metadata when needed; read the pointed-to answer in every case.

Read the final answer and execution metadata, then verify git status/diff
against the allowed write_set. A process exit code alone is not verification.
On failure, use `attempt_result_file` for produced/partial text instead of
mining events. Never promote an incomplete attempt to the successful result.

## Denials and warnings

`denials_observed` is advisory: a completed turn with a verified answer can
still be `ok` despite a refused tool call. A short answer warrants inspection;
confirm required checks actually ran, and disclose refusals that affect the
conclusion. A retry that succeeded does not erase earlier denials.

Structured denials come from explicit tool refusals, flags or runner errors
naming OS/sandbox denial. If Codex has no structured denial and its completed
answer starts by explicitly reporting shell calls failed with
`sandbox-exec: sandbox_apply: Operation not permitted`, the receipt adds a
single advisory labeled as a Codex self-report, not a verified tool event.
Ordinary command output is not evidence: tests printing “permission”,
`429` or “quota exceeded” must not become permission/quota failures.
`warnings` similarly records recovered CLI errors without changing status.
Failure classification uses structured CLI output, not the agent's answer or
command output.

Antigravity cannot request headless approval. A missing allow-rule can yield
an empty `SUCCESS` with `jetski: no output produced` on stderr. run.py treats
this as `permission`, not fallback-eligible `format`; inspect denied tools and
targets, then correct explicit access (e.g. `--add-dir` or an allow-rule)
without silently widening posture or switching models.
For shell sandbox failures, only the runner-composed `There were sandbox
errors` header is evidence; command stdout containing that text is not.
The turn may still complete, so check whether the required test ran.

For quota failures follow [quota-handover.md](quota-handover.md).
For option/protocol errors, read the relevant `agents/*.md` reference and run
its zero-token recovery before retrying. If a CLI upgrade needs maintenance,
inspect `maintenance_notice` and name the affected skill file to the user.
