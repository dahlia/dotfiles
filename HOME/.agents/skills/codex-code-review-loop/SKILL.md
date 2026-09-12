---
name: codex-code-review-loop
description: Narrow, single-reviewer workflow using codex exec --json, with self-review when the executing assistant is Codex. Use only when the user names this skill explicitly or asks for a Codex-only loop with no Claude pass. For general review-and-fix requests, use code-review-loop instead. Requires access to the target repository; external review also requires an authenticated Codex CLI.
---

Codex code review loop
======================

Codex reviews; you fix. Repeat until the review comes back clean.


Determine execution context
---------------------------

Determine your identity from the actual runtime, not from the presence or
absence of an MCP tool.

 -  If you are Codex and can access the target repository, use the self-review
    path.
 -  Otherwise, if a command-execution tool can reach the target repository and
    an installed, authenticated Codex CLI, use the external CLI path.
 -  If neither path is available, use the unavailable-environment path.

A shell in a cloud container does not imply access to the user's host
repository, Codex installation, or login. Check capabilities rather than
application names: Claude Desktop may have a host execution connector, or it
may have no such tool. Do not invoke the removed `codex mcp-server` command or
depend on Codex MCP tools.


Determine mode and scope
------------------------

From the target checkout, record the repository root and current state:

~~~~ bash
git rev-parse --show-toplevel
git status --short
~~~~

Uncommitted changes select pre-commit mode. Include staged, unstaged, and
relevant untracked files unless the user narrowed the scope. A clean tree
selects post-commit mode. Preserve unrelated work and any explicit staged-only
scope.

For post-commit mode, honor an explicit range first. Otherwise, on a feature
branch, find the merge base with the repository's default branch. On the default
branch, use the upstream tracking commit as the base for unpushed commits.
Resolve the base to a SHA and keep it fixed throughout the loop, including fix
commits. If the base is missing or the range is empty or ambiguous, report that
and ask for the intended scope; do not silently review an arbitrary commit.


External CLI review
-------------------

### Choose the model and prepare the prompt

Use the user's specified model, if any. Otherwise fetch
`https://developers.openai.com/api/docs/guides/latest-model.md` and resolve the
exact current frontier model ID supported by Codex. Record that ID for every
round and any `Assisted-by` trailer. If it cannot be established or the CLI
rejects it, report the blocker rather than silently substituting another model.

Check `codex exec --help` and `codex exec resume --help` for the installed
version. Create a fresh temporary directory outside the checkout for each
round's prompt, JSONL events, stderr, and final response. Set `review_dir`,
`review_root`, and `review_model` to the absolute temporary directory,
repository root, and exact model ID respectively.

Write `prompt.txt` using a file-writing tool or a quoted heredoc. Do not
interpolate prose into shell command text. Use this prompt with a concrete
scope:

~~~~ text
Review the changes in this repository. Review only; do not edit or commit files,
and do not start another review loop.

Review scope: [uncommitted staged, unstaged, and relevant untracked changes;
or the full diff from the fixed base SHA to HEAD, including fix commits]

Focus on correctness, security, maintainability, and edge cases within the
requested scope. Read the repository directly. For each actionable issue,
identify the file and line, explain the problem, and suggest a fix.

If you cannot inspect the full scope, report the limitation, not a clean result.
If you find no issues, respond with exactly: No issues found.
~~~~

Pass only the scope and review instructions, not pasted diffs or file contents.
The reviewer reads the checkout itself.

### Start the review

~~~~ bash
codex -a never exec --json --sandbox read-only \
  -C "$review_root" -m "$review_model" \
  -o "$review_dir/review.txt" - \
  < "$review_dir/prompt.txt" > "$review_dir/events.jsonl" \
  2> "$review_dir/stderr.log"
~~~~

Do not use `--ephemeral`: the loop needs a persistent review thread. Record the
`thread_id` from the `thread.started` JSONL event. Read the final review from
`review.txt`; stdout also contains progress and tool events and is not itself
the verdict.

A clean round requires all of the following: a successful process exit, a
`turn.completed` event with no error or failed-turn event, and a final response
whose trimmed contents are exactly `No issues found.`. Never grep the whole
stream for this sentence: it can occur in echoed prompts or intermediate output.
Authentication, quota, permission, transport, parsing, or incomplete-output
failures are blocked reviews, not clean reviews. Report the concrete error and
do not commit on the strength of a failed review or silently switch reviewers.

### Fix and re-review

Apply valid fixes within the original task and run checks appropriate to them.
Explain unsupported findings to the reviewer; do not widen the task just to
satisfy a finding. Track whether any fixes were applied.

In pre-commit mode, accumulate fixes without committing. In post-commit mode,
commit each fix batch before re-review, with the trailer described below.

Write a new prompt summarizing fixes or disputed findings and restating the full
scope. In pre-commit mode, request the current uncommitted diff. In post-commit
mode, request the complete fixed-base-to-HEAD diff, including new fix commits.
Ask the reviewer to verify earlier findings and look for new issues, using the
same exact clean sentinel.

Reuse the recorded `review_thread` UUID, never `--last`. Use fresh output paths
for each round so stale results cannot be accepted:

~~~~ bash
codex -a never exec --json --sandbox read-only \
  -C "$review_root" -m "$review_model" \
  resume --json -o "$review_dir/review.txt" "$review_thread" - \
  < "$review_dir/prompt.txt" > "$review_dir/events.jsonl" \
  2> "$review_dir/stderr.log"
~~~~

Keep execution options before `resume` where the installed CLI requires them.
Apply the same completion checks to every round. Continue until clean; if a
blocker or unresolved scope disagreement prevents progress, report it without
claiming the loop passed. Retain the thread UUID and relevant diagnostic paths
for resumption; remove temporary files when no longer needed.


Self-review when executing inside Codex
---------------------------------------

Read the full selected diff (`git diff HEAD` for tracked uncommitted changes,
`git diff --cached` for staged-only scope, or `git diff <base> HEAD` for a
committed range) and relevant surrounding code. Inspect in-scope untracked files
separately. For a repository without HEAD, inspect staged and working files
directly rather than treating a failed diff as an empty review.

Review correctness, security, maintainability, and edge cases. Fix issues within
scope, run appropriate checks, then re-read the full scope until no issues
remain. Use the same pre-commit and post-commit timing as the external path.
State that this was a self-review; do not present it as independent external
review.


Commits and attribution
-----------------------

Follow the user's commit constraints. When committing is allowed, pre-commit
mode ends with one commit of the reviewed changes after a clean review;
post-commit mode commits each fix batch before re-review. Do not include
unrelated changes or push implicitly.

If fixes were applied, include `Assisted-by: Codex:<exact-model-id>` on the
pre-commit result or each post-commit fix batch. Use the external reviewer's
selected exact model ID, or your actual runtime model ID for self-review. Do not
copy a stale example model ID or attribute work to a reviewer that did not run.


Unavailable environment
-----------------------

If you cannot run the CLI against the target checkout, say that the Codex review
loop has not run and explain the missing capability. A skill supplies
instructions; it cannot grant host terminal access. Do not install a CLI in an
unrelated cloud container or request copied login tokens as a workaround.

Provide a short handoff for a host-capable session, such as Claude Code or
Codex: the known repository path, requested review scope, current findings or
edits, and an instruction to run this skill there. Mark unknown paths or
revisions as unknown rather than inventing them. Do not claim a handoff was
executed merely because you prepared it.

You may inspect code already available in the conversation and report
provisional findings, but a Claude self-review does not satisfy a requested
Codex review gate. Do not claim a clean loop or make a gate-dependent commit.
If the user explicitly chooses a different reviewer or supplies host execution
access, follow that new instruction.
