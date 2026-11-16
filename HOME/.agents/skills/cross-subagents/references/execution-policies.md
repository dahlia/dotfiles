# Execution permissions and protected inputs

Read before choosing tool restrictions, external file access or `--guard`.

## Working directories and sources

Use repeatable `--add-dir PATH` for checkouts/files outside `--cwd`.
Containing directories of `--context-file` entries are granted automatically.
Antigravity auto-denies external reads even in bypass mode, Claude restricts
working directories, and OpenCode uses `external_directory` permissions.
Codex restricts writes, not reads, so needs no equivalent read grant.

`--allow-gh` permits issue/PR queries in read-only mode; `--allow-network`
permits web fetch/search. Antigravity lacks per-tool network controls, so these
flags have no effect there: use local snapshots instead. A source URL is not
proof that the child can access it. See [orchestration.md](orchestration.md),
“Writing subagent prompts”, for source-access details.

## Tool and sandbox posture

- `--sandbox workspace-write` is the default; `read-only` is for non-mutating
  reviews. Use `bypass` only when explicitly appropriate, for example when a
  permitted local test runner needs unrestricted execution. Do not silently
  widen permissions to recover a failed task.
- `--no-tools` implies read-only. Claude (`--tools ""`) and OpenCode (all-deny
  agent) enforce it; Codex and Antigravity rely on a prepended instruction and
  report `tools_enforced: false`. Failed attempts report null rather than
  claiming enforcement. Require `tools_enforced: true` for a hard no-tool gate.
- `--allow-shell` allows shell inspection/focused tests while editing tools
  stay denied in read-only mode. This does not guarantee commands succeed.
  Antigravity receives a session settings `command(*)` allow-rule but keeps
  its read-only filesystem sandbox.
- `--isolate` suppresses ambient behavior: Claude safe mode/strict MCP;
  OpenCode instructions/skill plugins/share/update/snapshots off; Codex rules
  off. Use for reviewers that must not inherit project hooks or skills.

Name the permitted `write_set` and explain why it exists. Concurrent subagents
must not collide through full test suites, shared schemas or lockfiles; run
focused tests or apply the scheduling ladder in orchestration.md.

## Protected-input guard

```bash
python3 <skill-dir>/scripts/run.py --run-id <run-id> --task-id <task-id> \
  --task code_review --sandbox read-only --allow-shell --isolate \
  --guard --guard-exclude 'node_modules/**' --guard-exclude 'dist/**' \
  --cwd <repo> --prompt-file <review-prompt.md>
```

The opt-in guard walks/hashes the tree before/after each attempt and writes
`attempt-N/guard/baseline.json` and `report.json`. Changes fail with `mutated`;
unverifiable baselines or cleanup fail with `guard_failed`. Neither falls
back to a different model reviewing a changed tree. If baseline reading fails,
the child never starts. Cache/artifact files are excluded even inside cwd.

Protected data includes contents, modes, symlink targets (including directory
symlinks), HEAD and its resolved branch, semantic index entries/persistent
flags, and merge/rebase markers. Unrelated refs, loose/packed ref storage
changes and index stat-cache refreshes do not invalidate the review.
Git-ignored untracked derived outputs are skipped by default; tracked files
remain protected even if ignored. `--guard-include-ignored` protects ignored
files too. It fails closed above `--guard-max-files` (default 100000).
Standalone usage: `python3 <skill-dir>/scripts/guard.py snapshot|verify`.

The guard is POSIX-only: Windows cannot reliably verify process groups and
rejects it. Unconfirmed process-group termination fails closed. It does not
cover object storage, reflogs, external symlink targets or nested repository
Git metadata. Daemonized/new-session descendants can escape the group check.
**This is a change detector, not a sandbox or proof of no transient writes.**
Pair it with read-only enforcement.
