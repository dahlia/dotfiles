Orchestration, concurrency, and safety reference
================================================

Fan-out threshold
-----------------

When a task dispatches 1 or 2 subagents, invoke `scripts/run.py` directly from
the current session and read the resulting `result.md` and `meta.json` files.

When a workflow requires 3 or more concurrent subagents, do not launch them
directly from the main session. Spawn a single orchestrator subagent first,
have that orchestrator run the parallel fan-out and aggregate the outputs, and
return only the synthesized report and file paths to the main session. This
keeps intermediate logs, retries, and large per-task outputs out of the primary
context window.


Live instance guard and partial restriction
-------------------------------------------

Fixed recursion depth limits fail to prevent wide fan-out explosions at depth 1
while unnecessarily blocking legitimate sequential delegation at depth 2.
Instead, `scripts/registry.py` enforces an atomic cap on concurrently live
instances per run under
`$SKILL_CACHE_DIR/registry/<run-id>/<instance-id>.json`:

~~~~ json
{
  "pid": 12345,
  "parent": "root",
  "started_at": "2026-10-28T12:00:00Z",
  "heartbeat_at": "2026-10-28T12:01:00Z"
}
~~~~

 -  Every `acquire`, `release`, or `status` call runs inside an exclusive file
    lock (`_registry.lock`) and reaps leases whose PID is no longer alive or
    whose `heartbeat_at` is older than 120 seconds. `scripts/run.py` runs a
    background heartbeat thread every 30 seconds while a child CLI executes.
 -  When the host agent enforces its own subagent cap and reports a refusal
    (such as Claude Code's `subagent_stats.refused`), pass `--host-refused` so
    the refused attempt is not double-counted.
 -  Reaching the live instance cap (default 6 across the run) does not crash or
    bypass the guard. `scripts/run.py` waits up to `--queue-timeout` for a free
    slot; if capacity remains full, it records `status: "deferred_capacity"`
    and `lease_mode: "sequential_inline"` so the caller executes sequentially.
 -  `scripts/run.py` exports `CROSS_SUBAGENTS_RUN_ID`,
    `CROSS_SUBAGENTS_PARENT_INSTANCE`, and `CROSS_SUBAGENTS_PARENT_TASK_ID` to
    child processes. When a subagent invokes `cross-subagents` recursively
    (`parent != "root"`), partial restriction applies: fan-out is disabled,
    each non-root parent is limited to at most 1 active child at a time within
    the global run cap, and nested delegation is permitted only when consulting
    a different model family for an independent opinion with the reason stated
    in the prompt. Nested calls inherit the run ID, so every descendant lands in
    the same `manifest.jsonl` and the root session can reconstruct the whole
    tree.


Writing subagent prompts
------------------------

A subagent starts with none of the root session's context. The prompt is all
it knows, so three things belong in every task prompt.

### Big-picture context and intent

State the larger goal, how this subtask contributes to it, and why the work is
needed. A subagent that only receives “add a null check to `parse()`“ will add
the null check. A subagent that also knows the null check exists to stop a
crash reported in issue #412 when config files are empty can notice that the
real crash is one call earlier and fix that instead, or at least say so. The
same holds for people: understanding the purpose of a task improves the result
and lets the assignee make things work when the instruction turns out to be
too narrow.

A short paragraph is usually enough:

~~~~ markdown
## Context
We are preparing the 2.4 release. Users on Windows report that `sync` hangs
when the target path contains spaces (issue #412). Your review feeds a
go/no-go decision for the release, so prioritize correctness bugs over style.

## Task
Review the diff in `fix-412.patch` ...
~~~~

### Concise prompts with file pointers

Keep the inline prompt short. `run.py` warns (`prompt_size_warning`) when a task
prompt exceeds 8,000 characters. Write large inputs (diffs, logs, specs,
previous subagent results) to files and pass them with `--context-file`;
`run.py` appends a pointer list to the prompt. The subagent needs read access to
those paths, which every sandbox mode grants for the working directory. Put
files outside the working directory under it or under
`$SKILL_CACHE_DIR/runs/<run-id>/` so read-only sandboxes can still reach them.

### Primary source pointers

When the root session works from an issue, a pull request, a discussion thread,
or a web page, pass each source with `--reference` so the subagent can dig
deeper when the summary you gave is not enough. Make sure the subagent can
actually open the pointer:

 -  `--allow-gh` lets read-only subagents run `gh issue`, `gh pr`, `gh api`,
    `gh search`, and `gh repo view` (Claude Code limits `Bash` to those
    prefixes; Codex and OpenCode enable shell and network for the sandbox).
 -  `--allow-network` lets read-only subagents use web fetch and web search
    tools.
 -  When neither is acceptable, save the content first (for example,
    `gh issue view 412 --comments > $SKILL_CACHE_DIR/runs/<run-id>/issue-412.md`)
    and pass the snapshot with `--context-file`.

Antigravity does not expose per-tool network controls in its CLI, so these two
flags have no effect on Antigravity routes; use saved snapshots there.

A pointer alone is passive. When you judge that reading the source will
materially improve the result (the issue thread contains reproduction steps,
a maintainer's design decision, or constraints the summary leaves out), pass
`--recommend-inspect`. `run.py` then tells the subagent to read the sources
before finishing, rather than leaving it to decide whether they matter.


Call tree, attribution, and significance
----------------------------------------

`run.py` records each task's parent (`parent_task_id`), its normalized
`official_agent` and `official_model_id`, a `task_summary`, an
`outcome_summary`, and a `significant` flag in `meta.json` and
`manifest.jsonl`. Pass `--summary` when invoking a task so the tree is readable.
The outcome summary defaults to the first substantive line of `result.md`, and
`significant` defaults to `true` for successful tasks and `false` for failed
ones.

Successful does not mean useful. A code reviewer that ran cleanly but found
nothing actionable did not contribute to the change. After reading each result,
correct the flag:

~~~~ bash
python3 <skill-dir>/scripts/run.py mark --run-id <run-id> --task-id <task-id> \
  --insignificant --outcome-summary "No actionable findings."
~~~~

At the end of a run, `run.py report --run-id <run-id> --human` prints the tree,
the deduplicated `(official_agent, official_model_id)` pairs, and
`Assisted-by:` trailers for significant pairs only. `--only-significant`
filters the JSON output the same way. Example output:

~~~~ text
Subagent Invocation Tree (run: release-2-4)
└── root
    ├── [review-a] Codex (gpt-6-astra) — status=ok, significant
    │   task: Review fix-412.patch for correctness
    │   outcome: Found off-by-one in path quoting on Windows
    │   └── [review-a-2nd] Claude Code (claude-fable-5-1) — status=ok, significant
    │       task: Independent check of the quoting finding
    │       outcome: Confirmed; proposed a minimal fix
    └── [review-b] OpenCode (glm-5.3) — status=ok, non-significant
        task: Review fix-412.patch for style and tests
        outcome: No actionable findings.

Recommended Commit Trailers (significant contributions only):
  Assisted-by: Codex (gpt-6-astra)
  Assisted-by: Claude Code (claude-fable-5-1)
~~~~

Report this tree to the user when the workflow finishes, unless the user asked
not to. If the repository has its own trailer format or crediting rule, adapt
the trailer lines to it; the normalized names stay the same.


Write-set scheduling ladder
---------------------------

Isolating every write task into a separate Git worktree adds unnecessary disk
and setup overhead when tasks touch unrelated directories. Schedule concurrent
writes using four escalating levels:

1.  Require each task specification to declare its `write_set` (the files or
    directory subtrees it may create or modify). When two subagents have
    non-overlapping `write_set` declarations and do not touch shared build
    artifacts, run them concurrently in the same working tree.
2.  When two tasks have overlapping `write_set` targets, grant a single-writer
    token to one subagent and run sibling subagents in read-only inspection
    mode (`--sandbox read-only`), producing patches or recommendations in their
    `result.md` for the writer or orchestrator to apply.
3.  Serialize edits to shared global files such as lockfiles
    (`package-lock.json`, `pnpm-lock.yaml`, `Cargo.lock`), database schemas, and
    generated code files so only one process updates them at a time.
4.  Isolate the subagent in a dedicated Git worktree (`git worktree add`) or
    standalone clone as a last resort when the write scope cannot be bounded in
    advance or when running full-repository test suites that conflict across
    processes.

Only the orchestrator (or main session) merges branches or reconciles
conflicting edits across tasks.


Coroutines versus one-shot pipelines
------------------------------------

Treat subagents as resumable coroutines rather than purely stateless
subroutines whenever multi-round interaction is likely:

 -  When keeping the same model across stages, preserve the session
    (`--keep-session` on turn 1, `--resume-task <task-id>` on later turns).
    Resume the stored conversation when supported. Context retention,
    compaction, and token accounting depend on the CLI and model.
 -  When switching model families across stages (such as Claude reviewing a
    patch drafted by Codex), pass the structured output file
    (`<task-id>/result.md`) and relevant file paths into a fresh session on the
    new route.
 -  One-shot tasks that will not be resumed should run in ephemeral mode (the
    default in `scripts/run.py`) so temporary subagent threads do not clutter
    the user's interactive session history.


Zero-token reactive monitoring vs. active polling loops
-------------------------------------------------------

When supervising asynchronous subagents, coordinators must never enter active
polling loops (such as running shell commands in a sleep loop like
`while ! test -f result.md; do sleep 5; done`, or prompting the LLM every few
seconds to check file status). Polling wastes conversational context, consumes
model input and output tokens on unproductive turns, and causes rate-limit
throttling.

Instead, leverage event-driven, zero-token reactive monitoring:

 -  **Host-Native Reactive Wakeup**: When launching background tasks in modern
    coding agent environments (such as Antigravity background tasks or
    subagent messaging), end the turn without calling further tools. The host
    system automatically wakes up the parent session upon process exit or
    message arrival, consuming zero tokens while the subagent runs.
 -  **Synchronous CLI Execution**: For linear subtasks, run `scripts/run.py`
    synchronously with an explicit wall-clock `--timeout`.
 -  **OS Synchronization Primitives**: In orchestrator scripts managing parallel
    subagents, use blocking primitives (such as `process.wait()`, file locks via
    `file_lock()`, or thread joins) rather than busy-wait loops.


Permissions and verification
----------------------------

Grant subagents enough local execution permission (`--sandbox workspace-write`
by default, `--sandbox read-only` for non-mutating inspection, or
`--sandbox bypass` where local test runners require unrestricted execution) to
inspect code and run targeted tests without hanging on interactive prompts.
Bound their behavior in the prompt (`assets/prompts/common.md` + task prompt)
and always explain *why* a boundary exists so the subagent can choose a safe
alternative if an edge case arises.

After every subagent finishes, verify its work before reporting success to the
user:

 -  Read `<task-id>/meta.json` and `<task-id>/result.md`.
 -  Run `git status --short` and `git diff` to confirm that modified files stay
    within the declared `write_set`.
 -  Check the actual test output rather than relying solely on the subagent's
    summary.


Cost gate and observability
---------------------------

Before launching a parallel fan-out, run:

~~~~ bash
python3 <skill-dir>/scripts/run.py --cost-gate <num_calls> --task <category>
~~~~

Print the one-line summary showing the planned call count, primary route, and
estimated cost index. If the fan-out involves 5 or more calls (or exceeds the
cost index gate), ask the user for confirmation before spawning the batch.

Every invocation appends `task_started` and `task_finished` events to
`$SKILL_CACHE_DIR/runs/<run-id>/manifest.jsonl`, and `run.py mark` appends
`task_outcome_marked`. When reporting back to the user, show the
`run.py report --human` tree, then summarize why routes were chosen, any
fallbacks or maintenance notices that occurred, and what commands or edits the
subagents performed.
