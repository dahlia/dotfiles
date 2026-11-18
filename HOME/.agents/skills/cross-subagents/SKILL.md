---
name: cross-subagents
description: Delegate tasks across heterogeneous coding agent CLIs (Codex, Claude Code, OpenCode, Antigravity) to run any model they expose—including GPT, Claude, Gemini, DeepSeek, Qwen, GLM, Kimi, MiniMax, MiMo, and other open-ended provider catalogs—as subagents with quota-aware, vision-aware, and speed-aware route selection, coroutine resumption, fan-out orchestration, and recursive call-tree attribution. Use this skill whenever the user asks to delegate work to a specific coding agent, model, or vendor ("run this with Codex", "ask Claude to draft this", "check with Gemini, DeepSeek, or Qwen"), wants independent cross-model verification or a second opinion from another model family, or needs to distribute parallel subagent tasks across multiple model subscriptions and quota pools. Do not use for single-agent tasks, simple lookups, or Orca terminal supervision (`orchestration`).
---

# Cross-subagents

Delegate across `codex`, `claude`, `opencode`, and `agy`. Discover authenticated
models dynamically: supported CLIs are fixed, model families are not.
Never hardcode a model-family allowlist.
The selection unit is a **route**, not a binary:
`(agent, provider, model_id, auth_plan, quota_bucket)`.
Different models/providers can share account quota; a model may consume
multiple buckets. If no cross-model heterogeneity is possible, tell the user
and do the work in the current session rather than paying delegation overhead.

## Runtime

Helpers require Python 3.11+, system IANA timezones (`tzdata` on Windows),
UTF-8 streams and list-based subprocess execution. Use `python3`, Windows
`python`, or `mise x python -- python`; if neither exists, ask for installation.
Do not upgrade user installations automatically.

`$SKILL_CACHE_DIR` means `$XDG_CACHE_HOME/cross-subagents` (default
`~/.cache/cross-subagents`) on Linux/macOS, or
`%LOCALAPPDATA%\cross-subagents` (default
`%USERPROFILE%\AppData\Local\cross-subagents`) on Windows.
Scripts use atomic writes, process locks, schema_version 2, and retain stale
data after failed refreshes. Pass `--refresh` when the user asks for refresh.
Never cache OAuth/API credentials, cookies or raw initialization dumps
containing user skill text.

## 1. Discover and select

```bash
python3 <skill-dir>/scripts/prepare.py --task <category> --human
```

Preparation performs discovery, open-defect checks and selection without
dispatching. It accepts all selector flags below; `--human` prints only routes,
rationales and actionable notices, with a full JSON `details_file` pointer.
Probe/model TTL is 24 hours; quota TTL is 15 minutes. Selection collects
candidate quota itself; no separate unscoped `quota.py status` is needed.
`--refresh` refreshes discovery only; `--refresh-quota` refreshes quota and is
incompatible with `--quota-cache-only`/`--quota-state`. Never replace
coordinator-owned quota with a live read. A confirmed exhausted bucket stays
blocked until its known reset even if the reading becomes stale. Claude Code
and OpenCode's `claude-agent` bridge share Claude subscription buckets; a
Fable-only limit does not block other Claude models. For individual diagnostics the
original probe.py, models.py, defects.py and select_route.py remain available.
Probe resolves mise shims and multiple PATH installations. OpenCode host and
target require v2+: surface v1 warnings, use another agent or request upgrade.
Avoid routes affected by open skill defects. If discovery needs verbose fresh
diagnostics, delegate that pass to a lightweight subagent.

Categories: `code_review`, `security_audit`, `architecture`, `debugging`,
`ui_debugging`, `frontend`, `code_generation`, `refactoring`, `testing`,
`prose`, `documentation`, `survey`, `general`.

Carry these requirements into selection **and execution**:

- Named agent/model: `--pin-agent`, `--pin-provider`, `--pin-model`.
- Independent opinion: `--exclude-vendor <host-vendor>`; frontier-only:
  `--include-vendor` and `--max-tier 2`.
- Screenshots/mockups/visual captures: always `--require-vision`.
- Fast turnaround: `--speed fast|urgent`; budget cap: `--max-cost-index`.
- Required sandbox, context size and modalities: filter before dispatch.
- Coordinator-owned quota: preserve `--quota-agent`, `--exclude-quota-agent`,
  `--quota-cache-only` or `--quota-state`; these limit reads, not eligibility.

Print the selection rationale: fit, vendor tier, limiting bucket/reset,
vision/speed when requested, and release age when decisive.
For flag semantics or multi-step plans, read
[selection-plans.md](references/selection-plans.md). Independent planned steps
are not fallback chains; save constraints with `--run-id` and execute each
step's selected route. For scoring/surveys read
[model-selection.md](references/model-selection.md), not for routine dispatch.

## 2. Choose native delegation or CLI

Prefer the host's native subagent mechanism when it supports the selected
exact model and required execution policy. Native tools avoid startup overhead
but cannot provide CLI session-suppression flags. Use `run.py` for another
agent/vendor or exact IDs, hygiene/resumption unavailable natively.
If external binaries are blocked by host policy, use permitted native/inline
execution and tell the user; do not bypass the policy.
Native receipts do not verify execution/model/permissions: retain host task
and completion/model evidence and register with `record-native` using
[reporting.md](references/reporting.md).

## 3. Prepare a concise prompt and execute

Include the larger goal and why this task matters, scope/write_set, requested
checks and expected output. Explain concurrency boundaries rather than merely
ordering compliance. Write task instructions once to a file and reuse them
across retries and parallel calls. Do not paste large sources/diffs/logs inline (8,000 characters
triggers a warning); pass `--context-file` pointers.

Give primary sources through `--reference`; use `--recommend-inspect` when
reading them materially improves quality. Ensure access with `--allow-gh`,
`--allow-network`, or local snapshots. Antigravity needs snapshots for these
network permissions. External checkouts need `--add-dir`; context-file
directories are granted automatically.

```bash
python3 <skill-dir>/scripts/run.py \
  --run-id <run-id> --task-id <task-id> --task <category> \
  --summary "<task description>" \
  --common-prompt <skill-dir>/assets/prompts/common.md \
  --prompt-file <task-prompt.md> --context-file <spec-or-diff> \
  --cwd <repo-or-work-dir>
```

Add `--route <agent>/<provider>/<model>` for an explicit selection.
Fallback must preserve persisted plan constraints; without a plan it retains
the canonical model within the vendor unless bounded substitution is explicit.
Use `--no-fallback` for exact-route-only gates. Do not silently downgrade,
change vendors, introduce metered billing or broaden permissions. On quota
failure `run.py` excludes the affected pool and reselects within the original
constraints. If a receipt has `quota_recovery.status: needs_coordinator`,
follow [quota-handover.md](references/quota-handover.md) in the same turn.
If no compatible route exists, ask the user about the closest available
replacement or waiting for reset; do not just summarize the failure.

Choose the least authority appropriate to the task:
`--sandbox read-only` for reviews, default `workspace-write` for scoped edits;
`bypass` only when actually authorized/needed. `--allow-shell` permits focused
tests; `--isolate` suppresses ambient hooks/skills.
`--no-tools` is **not enforced** by Codex/Antigravity: require a route with
`tools_enforced: true` for a hard gate.
`--guard` detects protected-input changes, **not a sandbox or proof of no
transient writes**; pair it with read-only enforcement.
Before using these policies, read
[execution-policies.md](references/execution-policies.md).

## 4. Verify, resume and report

Artifacts: `$SKILL_CACHE_DIR/runs/<run-id>/<task-id>/`.
After execution run `run.py inspect --run-id <run-id> --task-id <task-id>
--human`; read its pointed-to final or partial answer. Inspection summarizes
metadata without changing the receipt or deciding review quality. Missing or
unreadable receipts/results fail closed. Verify actual model, tool enforcement,
required checks, and git status/diff against write_set. The full metadata path
remains available for unprojected details. Denials/warnings may accompany
a successful answer: assess their effect, not just presence, and disclose
material limitations. On missing/incomplete results, denials or recovered
errors, read [results.md](references/results.md). Do not mine event logs first.

For multi-turn work, initial `--keep-session`, then a new task/prompt with
`--resume-task <initial-task-id>`. **Resume does not restore external read
grants or tool/isolation/guard policies**; read
[sessions.md](references/sessions.md) and repeat required options.

Use background completion notifications or synchronous execution with
`--timeout`. Never poll status/files through sleep loops or repeated LLM turns.
Parallel coordination uses blocking locks/joins, not active polling.

Evaluate significance from actual contributions, not success alone. Mark
no-actionable-findings reviews insignificant; use canonical IDs (DeepSeek
V4.1+ is `deepseek-flash`) and repository-specific attribution rules.

```bash
python3 <skill-dir>/scripts/run.py report --run-id <run-id> --include-defects --human
```

Present the full agent/model call tree, task outcomes and quota handovers
(original/replacement routes, reason, decision, changed requirements, outcome),
unless the user omitted the tree. Always disclose handovers and this run's
defect paths/severity/title. For mark/native/credit commands read
[reporting.md](references/reporting.md).

## Conditional references

Read only documents required by the current task, not this entire list.

| Situation | Read before proceeding |
|---|---|
| Selection flags, quota scope, independent step plans | [selection-plans.md](references/selection-plans.md) |
| Scoring, missing model surveys, reset-credit decisions | [model-selection.md](references/model-selection.md) |
| Permissions, tools, external paths, guard | [execution-policies.md](references/execution-policies.md) |
| Resume conversation | [sessions.md](references/sessions.md) |
| Artifacts, denied tools, incomplete/failed answer | [results.md](references/results.md) |
| Native receipt, significance, credit details | [reporting.md](references/reporting.md) |
| Skill defect encountered | [defects.md](references/defects.md) |
| Quota failure or replacement | [quota-handover.md](references/quota-handover.md) |
| 3+ concurrent agents, write scheduling or leases | [orchestration.md](references/orchestration.md) |
| CLI option/format/protocol failure | Relevant agent reference below |

Before **any fan-out**, run `run.py --cost-gate <num_calls> --task <category>`;
price the intended `--route` or saved `--run-id`, not a generic auto estimate.
Print the cost/call estimate and honor confirmation gates (including 5+ calls).
For 3+ concurrent agents, use one orchestrator returning a consolidated summary.
Schedule disjoint write_sets first, single-writer for overlaps, serialize
lockfiles/schemas, worktrees last. Read orchestration.md before that batch.

For CLI errors follow zero-token recovery before retrying:
[Codex](references/agents/codex.md),
[Claude Code](references/agents/claude-code.md),
[OpenCode](references/agents/opencode.md),
[Antigravity](references/agents/antigravity.md).
Inspect `maintenance_notice` and tell the user which skill file needs repair.
Record skill defects immediately using defects.md, continue safely, and do
not resolve reports before the skill is actually fixed.
