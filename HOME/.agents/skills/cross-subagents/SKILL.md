---
name: cross-subagents
description: Delegate tasks across heterogeneous coding agent CLIs (Codex, Claude Code, OpenCode, Antigravity) to run any model they expose—including GPT, Claude, Gemini, DeepSeek, Qwen, GLM, Kimi, MiniMax, MiMo, and other open-ended provider catalogs—as subagents with quota-aware, vision-aware, and speed-aware route selection, coroutine resumption, fan-out orchestration, and recursive call-tree attribution. Use this skill whenever the user asks to delegate work to a specific coding agent, model, or vendor ("run this with Codex", "ask Claude to draft this", "check with Gemini, DeepSeek, or Qwen"), wants independent cross-model verification or a second opinion from another model family, or needs to distribute parallel subagent tasks across multiple model subscriptions and quota pools. Do not use for single-agent tasks, simple lookups, or Orca terminal supervision (`orchestration`).
---

Cross-subagents
===============

This skill delegates tasks across installed coding agent CLIs (`codex`,
`claude`, `opencode`, `agy`) so a session running on one model family can call
any model exposed by those agents—including GPT, Claude, Gemini, DeepSeek, Qwen,
GLM, Kimi, MiniMax, MiMo, and newly released models discovered at runtime—as
subagents.

While the supported coding agent CLIs are fixed to `Codex`, `Claude Code`,
`OpenCode`, and `Antigravity`, model support is open-ended. Never hardcode an
allowlist of model families: discover routes dynamically from each CLI and allow
any authenticated model to participate in selection and execution.


Core unit: the route
--------------------

Do not reason at the granularity of an agent binary alone. Antigravity serves
Claude and GPT-OSS models alongside Gemini, OpenCode connects to half a dozen
providers at once, and Claude Code separates weekly quota between Fable and
other models. Every cache entry, capability survey, quota reading, and dispatch
decision in this skill keys on a route:

~~~~ text
route = (agent, provider, model_id, auth_plan, quota_bucket)
~~~~

Examples of distinct routes:

 -  `codex/openai/gpt-6-astra` (ChatGPT subscription, `codex:codex` weekly
    bucket)
 -  `claude-code/anthropic/claude-opus-5-5` (Claude Max, `claude-code:session`
    and `claude-code:all-models` buckets)
 -  `claude-code/anthropic/claude-fable-5-1` (Claude Max, `claude-code:session`,
    `claude-code:all-models`, and `claude-code:fable` buckets)
 -  `opencode/deepseek/deepseek-flash` (DeepSeek API key, `opencode:deepseek`
    bucket)
 -  `opencode/opencode-go/deepseek-v4.1-flash` (OpenCode Go subscription,
    `opencode:opencode-go` bucket; normalized official model ID:
    `deepseek-flash`)
 -  `opencode/opencode-go/glm-5.1` and `opencode/opencode-go/kimi-k2.6`
    (OpenCode Go subscription, `opencode:opencode-go` bucket)
 -  `antigravity/anthropic/claude-opus-5-5-high` (Antigravity subscription,
    `antigravity:default` shared bucket and `antigravity:model:claude-opus-5-5`
    individual bucket; normalized official model ID:
    `claude-opus-5-5`)

If only a single route is usable on the machine and no cross-model
heterogeneity is possible, tell the user and handle the task in the current
session rather than paying CLI delegation overhead.


Script execution and cache directory
------------------------------------

All helper scripts live in `<skill-dir>/scripts/` and require Python 3.11 or
later (`Linux`, `macOS`, `Windows`) with system IANA timezone data (or the
`tzdata` package on Windows for timezone-aware quota reset parsing), UTF-8
stream enforcement, and list-based subprocess execution.

Run scripts with `python3` (or `python` on Windows). If Python is not on
`PATH`, run them through `mise`:

~~~~ bash
mise x python -- python <skill-dir>/scripts/<script>.py ...
~~~~

If neither Python nor `mise` is installed, ask the user to install Python 3 or
`mise` before proceeding.

All cached state, model surveys, instance leases, and run artifacts live in
`$SKILL_CACHE_DIR`:

 -  Linux and macOS: `$XDG_CACHE_HOME/cross-subagents/` (defaults to
    `~/.cache/cross-subagents/` when `XDG_CACHE_HOME` is unset)
 -  Windows: `%LOCALAPPDATA%\cross-subagents\` (defaults to
    `%USERPROFILE%\AppData\Local\cross-subagents\`)

Every cache file uses atomic rename writes, cross-process file locking, and
`"schema_version": 2`. Never store OAuth tokens, API keys, session cookies, or
raw CLI initialization dumps containing user skill text in `$SKILL_CACHE_DIR`.
When a refresh fails after a TTL expires, the scripts retain the previous data
with `"stale": true` rather than discarding working state. Pass `--refresh`
whenever the user explicitly asks to refresh cached state.


Workflow
--------

### Step 1: Probe installed agents and discover routes

Check which coding agents are installed, authenticated, and version-compatible,
and populate the route catalog (24-hour TTL):

~~~~ bash
python3 <skill-dir>/scripts/probe.py --human
python3 <skill-dir>/scripts/models.py collect --human
python3 <skill-dir>/scripts/defects.py list --human
~~~~

The last command lists open defect reports about this skill; see
[skill defect reports](#skill-defect-reports-self-improvement-loop) for when to
add one and how to report it to the user at the end.

When multiple installations exist on `PATH` (for example, a `mise` install
alongside a system package), `probe.py` resolves `mise` shims and records all
candidate paths and the active version.

OpenCode v2 or later (`>= 2.0.0`) is required for both the host session
(`host_usable: true`) and target CLI invocation. If OpenCode v1 (`< 2.0.0`) is
installed, `probe.py` marks it unusable (`usable: false`, `target_usable: false`)
and records a warning; surface that warning to the user and select another
agent, or ask the user to upgrade. Never upgrade the user's installation
automatically.

If route collection or quota inspection needs to be run from scratch and would
produce verbose diagnostic output, delegate the discovery pass to a lightweight
subagent so the main session context stays clean.

### Step 2: Check account quota and select a route

Refresh quota readings (15-minute TTL) and select the primary route and
fallback chain:

~~~~ bash
python3 <skill-dir>/scripts/quota.py status --human
python3 <skill-dir>/scripts/select_route.py --task <category> --human
~~~~

Supported `--task` categories: `code_review`, `security_audit`, `architecture`,
`debugging`, `ui_debugging`, `frontend`, `code_generation`, `refactoring`,
`testing`, `prose`, `documentation`, `survey`, and `general`.

Useful selection flags:

 -  `--pin-agent <agent>`, `--pin-provider <provider>`, `--pin-model <model>`
    restrict selection when the user names a specific agent or model. Add
    `--save-pin` (and `--run-id <run-id>` to scope pins to the active run) if
    the user wants that constraint kept across subsequent calls.
 -  `--exclude-vendor <vendor>` (such as `openai`, `anthropic`, `google`,
    `deepseek`, `qwen`, `zhipu`, `moonshot`, `minimax`, or `xiaomi`) requires a
    model vendor different from the host session for independent verification.
 -  `--include-vendor <vendor>` (repeatable) restricts selection to the named
    vendors instead of listing every other vendor in `--exclude-vendor`.
 -  `--max-tier <N>` keeps only models ranked at or above N within their own
    vendor (`1` = flagship, `2` = second tier). Ranking is computed per vendor
    from task-fit evidence and is independent of quota, cost, and speed, so a
    tight account does not silently demote a vendor's flagship to its third
    model. Use `--max-tier 2` when you want "top or second tier" rather than a
    specific model.
 -  `--max-cost-index <float>` (0.0–1.0) caps the normalized per-route cost
    index after billing-mode adjustment. Use it for budget-sensitive work such
    as prose copy-editing: `0.10` keeps mid-tier models (for example Sonnet,
    Terra, Gemini Flash, or open-weight vendors) and excludes flagship-class
    routes such as GPT-6 Astra or Claude Fable. The selector's cost term is
    only a soft weight, so without this cap the highest-fit flagship usually
    wins. The cap can also be set per step with `max_cost_index` in
    `--step-spec`.
 -  `--exclude-route <route_id>` (repeatable) removes an exact route, which is
    how a step plan keeps later steps on different models.
 -  `--require-vision` (or `--require-modality image`) restricts selection to
    multimodal models that accept image inputs. Always pass `--require-vision`
    when the subagent needs to inspect UI design mockups, screenshots, or
    Playwright visual captures during frontend or UI debugging work.
 -  `--speed <normal|fast|urgent>` (or `--prefer-fast` for `fast`) weights token
    generation throughput (`tokens_per_sec`) and latency tier into route
    selection when the user wants a rapid turnaround.
 -  Recency is scored automatically: a route's release date (from a survey's
    `released_at`, the local OpenCode model registry, or family priors) turns
    into a small bonus for recent models and a small penalty for stale ones.
    Six months is the neutral point, so a model released half a year ago
    neither gains nor loses; unknown release dates stay neutral. No flag is
    needed. Recency should break close calls in favor of the newer model, not
    override a clearly stronger one, so mention the release age in the
    rationale when it decided the outcome.
 -  `--min-headroom <float>` (default `0.15` for heavy tasks) and
    `--allow-micro-quota` prevent dispatching heavy subagent tasks to low-tier
    or free plans that lack the absolute token headroom to survive the run, even
    if currently reporting 100% remaining quota.
 -  `--min-context <tokens>` and `--require-modality <modality>` filter out
    routes that do not meet verified capability requirements (pass
    `--allow-estimated-capabilities` when family-prior estimates are
    acceptable).
 -  `--dry-run` prints the selected route, one-line rationale, and fallback
    chain without executing a subagent.
 -  `--sandbox <read-only|workspace-write|bypass>` excludes routes whose
    installed CLI cannot honour the posture. Older Antigravity builds without
    `--mode`/`--sandbox` are skipped for read-only and workspace-write work
    instead of wasting an attempt; pass the same value to `run.py`.

Always print the one-line selection rationale so the user can see which route
was chosen and why (task fit, release age when it decided the result,
vision/speed traits when requested, vendor tier, limiting quota bucket usage,
and time until reset). For the scoring formula, quality floor rules, recency and
token-speed weighting, reset-imminence trade-offs, reset credit redemption
policy, and canonical model survey workflow (`models.py survey-missing` and
`survey-put`), read the [model selection reference](references/model-selection.md).

#### Multi-step plans

A multi-step workflow (for example a cheap first-pass reviewer followed by two
independent frontier reviewers) needs *independent* routes, not a fallback
chain. Plan them in one call:

~~~~ bash
python3 <skill-dir>/scripts/select_route.py --task code_review --steps 3 \
  --step-spec '{"speed":"fast","quality_floor":0.60,"exclude_vendor":["openai","anthropic","google"]}' \
  --step-spec '{"include_vendor":["openai","anthropic","google"],"max_tier":2}' \
  --step-spec '{"include_vendor":["openai","anthropic","google"],"max_tier":2}' \
  --human
~~~~

Each `--step-spec` is inline JSON or `@path/to/spec.json`; omitted steps inherit
the global flags. Global `--exclude-route`, `--exclude-vendor` and
`--exclude-agent` entries are additive, so a step spec cannot re-enable a route
the caller excluded for the whole plan. Steps are independent: a route already
chosen by an earlier step is excluded, and `--step-distinct-vendor` (the
default) forces each step onto a different model vendor so the later steps are
genuinely independent checks. `--step-distinct-bucket` additionally spreads steps across quota
buckets when two vendors share one subscription. Per-step `fallbacks` remain
available for failure of that step's own model, but they do not replace a
planned step. Plan mode ignores saved session pins; pass `--pin-*` explicitly
only when a single route is intended. With `--run-id <run-id>`, the plan's
effective per-step constraints are persisted for `run.py` so explicit-route
fallbacks cannot escape the filters that produced the plan.

`--steps` is a plan, not an execution: pass each step's `selected.route_id` to
`run.py --route` for that step's own rounds. When the plan is created with
`--run-id <run-id>`, `select_route.py` also saves the per-step selection
constraints to `<run-dir>/selection-plan.json`; `run.py` reuses them so a
failed step can fall back only inside the same filtered chain (for example, a
plan that excluded Anthropic and OpenAI can never fall back to Claude or GPT).
Without a saved plan, an explicit `--route` restricts its fallback chain to the
primary route's own vendor unless `--include-vendor` names a wider allow-list;
pass `--no-fallback` when even that is unacceptable.

### Step 3: Choose native delegation or CLI execution

When the selected route's exact model is supported directly by the current host
agent's built-in subagent mechanism (`Task` in Claude Code or OpenCode,
prompt-level subagent delegation in Codex, or `invoke_subagent` in Antigravity
when a relative Gemini tier suffices), prefer native delegation over spawning
an external CLI. Native delegation avoids CLI startup overhead and shares host
context directly, though it cannot pass CLI session-suppression flags
(`--ephemeral` or `--no-session-persistence`). If the host environment's
security policy blocks spawning external agent binaries, fall back to native
delegation or inline execution and inform the user.

Whenever the selected route belongs to a different agent or model vendor, or
requires an exact model ID, session hygiene, or resumable CLI state not exposed
by the host's native tool, execute it through `scripts/run.py`.

### Step 4: Craft context-rich, concise prompts and invoke `run.py`

Follow three rules when writing every subagent task prompt:

1.  **Include big-picture context and intent.** Do not send bare, mechanical
    commands. State what larger goal the subagent's output serves, how its
    subtask fits into the overall effort, and *why* the work is needed. Whether
    the assignee is a person or an LLM, understanding the broader objective
    improves output quality and lets the subagent make sensible decisions when
    a narrow instruction turns out to be myopic or hits an unexpected edge case
    in the codebase.
2.  **Keep prompts concise by passing file pointers (`--context-file`).** Avoid
    bloating the task prompt (>8,000 characters triggers a `prompt_size_warning`
    in `meta.json`). Never paste large source files, diffs, or logs inline.
    Instead, save or reference them as separate files, grant the subagent file
    read access (`Read`), and pass their paths via `--context-file <path>`.
    Directories containing those context files are granted to agents that scope
    tool access to the working directory, so external snapshots stay readable.
3.  **Provide primary source pointers (`--reference`) and recommend inspection
    when valuable (`--recommend-inspect`).** When the root session relies on
    issue tracker items (issues, related issues, or discussions), pull requests,
    or web pages, give the subagent direct pointers to those sources so it can
    gather upstream context on demand. Enable access either by passing
    `--allow-gh` (allowing `gh` CLI queries even in `read-only` mode) or
    `--allow-network` (allowing web fetch/search), or by saving the upstream
    issue/PR/web content to a local snapshot file beforehand and passing its
    path. When the coordinator judges that reading those primary sources will
    materially improve task quality, pass `--recommend-inspect` (and state so in
    the prompt) to actively advise the subagent to read the primary material
    rather than treating the pointer as passive metadata.

Never paste or generate the same prompt twice across retries or parallel calls.
Write the task instructions to a file first, combine them with
`<skill-dir>/assets/prompts/common.md`, and let `scripts/run.py` stream the
combined prompt to the child CLI over standard input:

~~~~ bash
python3 <skill-dir>/scripts/run.py \
  --run-id <run-id> \
  --task-id <task-id> \
  --task <category> \
  --summary "<concise description of the delegated task>" \
  --common-prompt <skill-dir>/assets/prompts/common.md \
  --prompt-file <path-to-task-prompt.md> \
  --context-file <path-to-detailed-spec-or-diff> \
  --reference "https://github.com/org/repo/issues/123" \
  --recommend-inspect \
  --allow-gh \
  --cwd <repo-or-work-dir>
~~~~

You can also pass `--route <agent>/<provider>/<model>` to target a specific
route while still allowing `run.py` to fall back if the primary route fails with
a quota, authentication, or model-availability error. The fallback chain stays
inside the run's persisted selection plan when one exists; otherwise it is
limited to the primary route's own vendor unless `--include-vendor` gives a
wider allow-list. Pass `--no-fallback` when a user gate requires that exact
model or nothing.

When the task prompt points at checkouts or files outside `--cwd` (a dependency
checkout, a sibling repository, a scratchpad snapshot), pass `--add-dir <path>`
(repeatable). Antigravity's headless mode auto-denies `read_file` for paths
outside the workspace in every sandbox posture and returns an empty `SUCCESS`
turn, Claude Code restricts tool access to its working directories, and OpenCode
rejects external paths through its `external_directory` permission; `run.py`
grants the roots through each CLI's mechanism. Directories containing
`--context-file` entries are added automatically. Codex needs no equivalent
because its sandbox restricts writes, not reads, so the directory is already
readable there.

Three flags shape the execution posture:

 -  `--no-tools` is the tool-free posture for prose and other text-only review.
    It implies `--sandbox read-only`. Claude Code (`--tools ""`) and OpenCode (a
    dedicated all-deny agent) enforce it; Codex and Antigravity have no CLI
    switch that disables tools, so they report `tools_enforced: false` in
    `meta.json` and rely on a prompt instruction prepended by `run.py`. A failed
    attempt reports `null` (unknown) rather than claiming enforcement. Choose a
    route whose `tools_enforced` is `true` when "no tool call happened" must be
    guaranteed.
 -  `--allow-shell` keeps editing tools denied but permits shell commands in
    read-only mode, for inspection and focused tests in a code review.
    Antigravity needs a session `--settings` allow-rule for this, which
    `run.py` supplies; its `--sandbox` still enforces a read-only filesystem.
 -  `--isolate` suppresses ambient behavior in read-only execution: Claude safe
    mode and strict MCP, OpenCode instruction/skill plugins and
    share/update/snapshots off, Codex rules off. Use it for reviewers that must
    not inherit project hooks or skills.

`run.py` writes all artifacts under `$SKILL_CACHE_DIR/runs/<run-id>/`:

 -  `prompts/common.md` and `prompts/task-<task-id>.md` store the prompt inputs
    and are reused automatically if a fallback attempt is needed.
 -  `<task-id>/attempts/attempt-<N>/` isolates raw stdout, stderr, and event
    logs per attempt so a failed retry never overwrites a prior attempt's trace.
 -  `<task-id>/result.md` holds the verified final response text from the
    winning attempt.
 -  `<task-id>/meta.json` records structured execution metadata matching
    [meta.schema.json](assets/schemas/meta.schema.json) (`status`, `exit_code`,
    `duration_ms`, `requested_model`, `observed_model`, `session_id`,
    `resumable`, `session_cleaned_up`, `usage`, `error_class`,
    `selection_rationale`, `tool_policy`, `tools_enforced`,
    `denials_observed`, `warnings`, `attempt_result_file`, `model_verified`,
    and `guard`).
 -  `manifest.jsonl` maintains an append-only audit log of `task_started` and
    `task_finished` events across the run.

#### Denials, failed attempts, and reading the answer

`denials_observed` is advisory. It lists tool calls the CLI explicitly refused
(a denied/declined status, a denial flag, or a tool-runner error naming an OS or
sandbox denial such as `EPERM`/`EACCES`). A completed turn with a verified final
answer is still `status: ok` even when the list is non-empty; only an incomplete
turn or a missing answer fails. Ordinary command output is never treated as
evidence of a denial, so a failing test or a file that happens to contain the
word "permission" cannot fail a task. Inspect the list when the answer looks
short, and tell the user when a refusal plausibly changed the result.

Antigravity's headless mode cannot open an approval prompt, so a tool call not
covered by an allow-rule is auto-denied; the turn still ends with
`status: SUCCESS` and an empty response, and the only evidence is a
`jetski: no output produced` note on stderr. `run.py` classifies that as
`permission` (not the fallback-eligible `format`), records the denied tool and
target in `denials_observed`, and stops instead of switching to another model,
because the gap is deterministic. Re-run with `--add-dir <path>` for a read, or
add an Antigravity `permissions.allow` rule. `--allow-shell` is granted
automatically through a session `--settings` override (`command(*)`); the
sandbox still mounts a read-only filesystem in read-only mode.

When an attempt fails but did produce text (for example a turn that stopped
before `turn.completed`), `meta.json` points at it with `attempt_result_file`
(`attempts/attempt-<N>/result.md` or `partial_result.md`). Read that file
instead of mining `events.jsonl`. The task-level `result.md` is published only
for a successful attempt.

`warnings` is the same idea for non-denial noise: CLI error events, or an
earlier step's error, that a later verified completion recovered from. These do
not change `status`. Failure classification (`error_class`) uses only structured
CLI error output—stderr, error events, or the CLI's own error payload—never the
agent's transcript or command output, so a failing test that prints `429` or
`quota exceeded` cannot mark an account as exhausted.

### Step 5: Manage coroutines, permissions, and verification

By default, `run.py` runs in one-shot mode and suppresses or deletes child
session records where the CLI supports it (`--ephemeral` on Codex,
`--no-session-persistence` on Claude Code, and verified
`opencode session delete --standalone` on OpenCode) so subagent calls do not
pollute the user's interactive session picker. Antigravity (`agy` v0.2.2) always
persists conversation state under `~/.gemini/antigravity-cli` and has no session
delete subcommand, so `run.py` records `session_cleaned_up: false` in
`meta.json`.

When a workflow needs multi-turn interaction with the same subagent (such as
iterative review loops or staged design refinement), treat the subagent as a
coroutine:

1.  Pass `--keep-session` on the initial call so the session persists and its
    full execution state (`session_id`, `route_id`, `effort`, `sandbox_mode`,
    `cwd`, and `resumable: true`) is recorded in
    `$SKILL_CACHE_DIR/runs/<run-id>/sessions.json`.
2.  Resume the paused session on a later turn with `--resume-task <task-id>`
    and a new follow-up prompt file:

~~~~ bash
python3 <skill-dir>/scripts/run.py \
  --run-id <run-id> \
  --task-id <followup-task-id> \
  --resume-task <initial-task-id> \
  --prompt-file <followup-prompt.md>
~~~~

Resume the stored conversation when supported. Context retention, compaction,
and token accounting depend on the CLI and model. Summarize and start a fresh
session when switching to a different model family between stages or when prior
turn history is no longer needed.

Give subagents enough execution permission (`--sandbox workspace-write`, the
default, `--sandbox read-only` for non-mutating reviews, or `--sandbox bypass`
when local test runners require unrestricted execution) to inspect files and run
targeted unit tests. Constrain their scope in the task prompt by naming the
allowed `write_set` and explaining *why* a boundary exists (for example,
explaining that sibling subagents are running concurrently and that full test
suites collide across processes, so only targeted unit tests should be run
unless isolated in a separate worktree).

After `run.py` completes, always read `<task-id>/meta.json` and
`<task-id>/result.md`, and verify `git status --short` and `git diff` against
the task's `write_set`. If the task failed, read the path in
`meta.json.attempt_result_file` when present, and review `denials_observed`
before deciding whether to retry or fall back.

#### Optional protected-input guard

For a read-only review where a silent edit would invalidate the verdict, pass
`--guard`. `run.py` hashes the working tree and Git state before the attempt,
verifies them afterwards, and writes `attempt-<N>/guard/baseline.json` and
`report.json`. A changed, added, or removed file, or a protected Git state change,
turns the attempt into `status: failed` with `error_class: mutated` (or
`guard_failed` when verification itself could not run); the task then stops
instead of falling back and reviewing a different tree. If the baseline cannot
be read (unreadable file or directory, unreadable Git metadata, or too many
files), the agent does not run at all. The skill's cache directory and its own
artifacts are excluded from the snapshot even when the cache lives inside
`--cwd`.

~~~~ bash
python3 <skill-dir>/scripts/run.py --run-id <run-id> --task-id <task-id> \
  --task code_review --sandbox read-only --allow-shell --isolate \
  --guard --guard-exclude 'node_modules/**' --guard-exclude 'dist/**' \
  --cwd <repo> --prompt-file <review-prompt.md>
~~~~

The guard is opt-in and costs one tree walk plus hashing per attempt. It
records file contents, modes, symlink targets (including directory symlinks),
HEAD and its resolved current branch, the index, and the contents of in-progress
merge/rebase markers. Unrelated shared refs and loose/packed ref storage changes
do not invalidate the review. Git-ignored untracked paths (`dist/`,
`.svelte-kit/`, compiled test bundles) are skipped by default, because a read-only
reviewer that runs a focused build regenerates them and they are derived artifacts, not protected
inputs; tracked files stay protected even when an ignore pattern matches them.
Pass `--guard-include-ignored` to protect ignored files as well. An attempt
whose process group cannot be confirmed gone is treated as `guard_failed`,
because a surviving descendant could still write after verification. It does
not cover Git object storage,
reflogs, external symlink targets, or the Git metadata of nested repositories,
and it fails closed above `--guard-max-files` (default 100000). It can also run
standalone: `python3 <skill-dir>/scripts/guard.py snapshot|verify`. The guard is
POSIX-only: Windows has no reliable process-group check, so `--guard` is
rejected there rather than reporting a clean run it cannot verify.

The guard is a change detector, not a sandbox. It catches ordinary background
children, but a descendant that creates a new session or process group
(`setsid`, daemonization) escapes the process-group check and can still write
after verification. Pair it with a read-only posture; a clean report is not
proof that no transient write occurred.

### Zero-token reactive monitoring vs. active polling loops

Never run active polling loops (such as
`while ! test -f result.md; do sleep 5; done` or recurring prompt turns that
repeatedly query `run.py report` to check task status). Polling consumes LLM
context, wastes input/output tokens, and risks model rate limits.

Instead, rely on event-driven reactive monitoring:

 -  **Host Agent Reactive Wakeup**: When launching `run.py` or subagents in the
    background, rely on the host system's built-in background task completion
    notifications. The host agent automatically wakes up when the process exits
    or when a subagent sends a message.
 -  **Synchronous Direct Execution**: For linear workflows, invoke `run.py`
    synchronously with a wall-clock `--timeout`.
 -  **Process Signals and Locks**: For parallel coordination, use OS blocking
    primitives (such as file locks or process joins) rather than shell sleep
    loops.

### Step 6: Track recursive lineage, filter significance, and report the call tree

Every `run.py` invocation propagates `CROSS_SUBAGENTS_RUN_ID`,
`CROSS_SUBAGENTS_PARENT_INSTANCE`, and `CROSS_SUBAGENTS_PARENT_TASK_ID` to child
processes so nested subagent calls form a complete parent-child tree under the
root session.

In `meta.json`, `manifest.jsonl`, and `run.py report`, every participating
`(official_agent, official_model_id)` pair is strictly normalized:

 -  **Official coding agent names** use exact official casing and spacing:
    `Codex`, `Claude Code`, `OpenCode`, `Antigravity` (never `OpenAI Codex`,
    `ClaudeCode`, `Open Code`, or `opencode`).
 -  **Official model identifiers** use the canonical lowercase model ID
    consisting of lowercase letters, digits, hyphens, and dots: for example,
    `gpt-6.1-sol`, `claude-fable-5-1`, `deepseek-flash`, `glm-5.3`,
    `claude-sonnet-5`, `claude-opus-5-5`. Normalization strips provider prefixes
    (`opencode-go/glm-5.3` -> `glm-5.3`), context-window suffixes
    (`claude-sonnet-5[1m]` -> `claude-sonnet-5`), effort suffixes
    (`claude-opus-5-5-low` -> `claude-opus-5-5`), release-date suffixes, and
    DeepSeek V4.1+ version tags (`deepseek-v4.1-flash` -> `deepseek-flash`), and
    resolves shorthand aliases (`fable` -> `claude-fable-5-1`) while rejecting
    non-existent bare prefixes (`gpt-6`).

When evaluating subagent outputs (for example, deciding which models to credit
in `Assisted-by` commit trailers under a repository rule that excludes code
reviewers with no actionable findings), update each task's outcome summary and
significance flag:

~~~~ bash
# Mark a review task that found no actionable issues as non-significant:
python3 <skill-dir>/scripts/run.py mark \
  --run-id <run-id> \
  --task-id <task-id> \
  --insignificant \
  --outcome-summary "Reviewed diff; reported no actionable issues."

# Register a native host subagent execution into the run tree:
python3 <skill-dir>/scripts/run.py record-native \
  --run-id <run-id> \
  --task-id task-host-native \
  --agent Antigravity \
  --model gemini-2.5-pro \
  --summary "Generated frontend components natively." \
  --outcome-summary "Produced button and modal components with tests."
~~~~

When the `cross-subagents` workflow completes, run `run.py report` and present
the full tree of participating coding agents and official model IDs—along with
what each task did and its outcome—to the user (unless the user explicitly asked
to omit the summary):

~~~~ bash
python3 <skill-dir>/scripts/run.py report --run-id <run-id> --human
# Filter to significant contributions only (e.g. for Assisted-by trailers):
python3 <skill-dir>/scripts/run.py report --run-id <run-id> --only-significant
~~~~

Then check for defect reports recorded during this run and include them in the
same final message, as described in
[skill defect reports](#skill-defect-reports-self-improvement-loop):

~~~~ bash
python3 <skill-dir>/scripts/defects.py list --run-id <run-id> --human
~~~~

If the list is non-empty, name each report path, its severity, and a one-line
title so the user can fix the skill without hunting for the files.


Multi-agent fan-out and orchestration
-------------------------------------

When a task requires 3 or more concurrent subagents, or when you need write-set
scheduling across parallel tasks, cost gating, or recursion lease management
via `scripts/registry.py`, read the
[orchestration reference](references/orchestration.md) before launching the
batch:

 -  Run
    `python3 <skill-dir>/scripts/run.py --cost-gate <num_calls> --task <category>`
    before any fan-out. Print the estimated call count and cost index, and ask
    for user confirmation when dispatching 5 or more calls. The estimate follows
    the plan you intend to run: pass `--route <agent>/<provider>/<model>` for a
    specific model or `--run-id <run-id>` to price the saved selection plan
    (`selection_source: explicit_route|saved_plan`). Without either, the result
    is labelled `auto` and its `note` says the index is a generic representative
    route, not the work plan; treat it as a rough gate, not a route estimate.
 -  For 3 or more parallel subagents, spawn one orchestrator subagent to manage
    the fan-out and return a consolidated summary so the main session context
    stays clean.
 -  Schedule concurrent writes via the write-set ladder: disjoint `write_set`
    directories in place, single-writer token when sets overlap, serialized
    access for lockfiles and schemas, and Git worktrees only as a last resort.


Skill defect reports (self-improvement loop)
---------------------------------------------

This skill maintains itself through the sessions that use it. Whenever an
execution reveals a defect *in the skill*—not in the user's repository or in the
model's answer—record it immediately in a report file and continue with the best
available workaround. At the end of the workflow, tell the user that reports
exist and where they are.

Record a report when, for example:

 -  a helper script exits with a traceback, writes a wrong `status`/`error_class`,
    or contradicts its documented behavior;
 -  a documented flag or option is rejected by an installed CLI, or an adapter
    misparses a stream/export and misreports the result;
 -  route selection returns a route that cannot honor the requested posture
    (`--sandbox`, `--no-tools`, vision), or a model that the user can actually
    reach is missing from the catalog;
 -  a workaround had to be invented for something the reference docs claim is
    handled, or a fallback burned quota because the primary route was
    misclassified.

Do not record model-quality complaints, failures caused by the user's code, or
ordinary quota/timeout errors: those already have an `error_class` and belong to
the run report.

~~~~ bash
python3 <skill-dir>/scripts/defects.py new \
  --title "Codex marks completed turns failed after a failing test" \
  --summary "A completed Codex turn was reported failed because a failing test printed 'permission'." \
  --severity blocker \
  --area "scripts/run.py _tool_denial" \
  --step "Step 4: invoke run.py" \
  --observed "status=failed, error_class=permission, error='Empty result from Codex.'" \
  --expected "status=ok with the verified final answer" \
  --reproduction "<exact command>" \
  --impact "Discarded 3 of 3 answers and wasted fallback quota." \
  --evidence "$SKILL_CACHE_DIR/runs/<run-id>/<task-id>/attempts/attempt-1/events.jsonl" \
  --run-id <run-id> --task-id <task-id>
~~~~

`new` prints the report path; `--json` prints `{id, path, severity, title}`.
Reports live in `$SKILL_CACHE_DIR/defects/` (override with
`CROSS_SUBAGENTS_DEFECT_DIR`) and use `blocker`, `major`, or `minor` severity.
Keep each report short, one defect per file, and point at evidence paths instead
of pasting logs. Never put tokens, prompts containing user secrets, or
proprietary code in a report.

At the start of a workflow, check for open reports and route around any that
affect the planned work:

~~~~ bash
python3 <skill-dir>/scripts/defects.py list --human
~~~~

At the end of the workflow, list the reports recorded for this run and tell the
user once, with the paths:

~~~~ bash
python3 <skill-dir>/scripts/defects.py list --run-id <run-id> --human
~~~~

For example: "2 cross-subagents defect reports were created: `<path-a>` (blocker,
...), `<path-b>` (minor, ...). Read them with `cat`; fix them and run
`defects.py resolve --id <id> --note "<fix>"`." Pass the workflow's run ID to
`defects.py new` so this filter is exact. Do not omit the notice when reports
exist, and do not block the task to fix the skill unless the user asks.

Only run `resolve` after the skill file has actually been fixed; it moves the
report to `$SKILL_CACHE_DIR/defects/resolved/` and appends the note.


Self-healing and CLI reference docs
-----------------------------------

When an agent CLI invocation fails with an option, format, or protocol error,
consult the agent-specific reference and run its zero-token recovery procedure
before retrying:

 -  [Codex CLI reference](references/agents/codex.md) covers `--no-daemon` root
    flag placement, `codex app-server` JSON-RPC pagination, and
    `generate-json-schema` recovery.
 -  [Claude Code CLI reference](references/agents/claude-code.md) covers
    `control_request` initialize with `--verbose`, `/usage` multi-bucket
    parsing, tool sandboxing flags, and `--resume` rules.
 -  [OpenCode CLI reference](references/agents/opencode.md) covers the v2
    version gate, `--standalone`, `#<variant>` syntax, `OPENCODE_CONFIG_CONTENT`
    read-only permissions, `session export`, and `session delete`.
 -  [Antigravity CLI reference](references/agents/antigravity.md) covers
    `agy models` TSV effort normalization, `stream-json` stdin prompt delivery,
    and `--print` argument ordering.

If a CLI upgrade breaks a hardcoded flag or RPC method and automatic repair
either adapts the call or cannot recover, inspect `maintenance_notice` in
`meta.json` and explicitly notify the user which file under
`~/.agents/skills/cross-subagents/` needs maintenance.
