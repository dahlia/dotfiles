---
name: fable-code-review-loop
description: Run an iterative code review loop using Claude Code in non-interactive mode with Claude Fable 5 — Fable reviews the code and you apply fixes, repeating until Fable finds nothing more to fix. Use this skill whenever the user asks to "run a Fable review", "do a Fable code review loop", "let Claude review my code", "Fable로 코드 리뷰", "Claude로 코드 리뷰", "코드 리뷰 루프", or wants an independent Claude Fable 5 review of uncommitted changes or a branch followed by iterative fixes.
---

Fable Code Review Loop
======================

Fable reviews; you fix. Repeat until the review comes back clean.

This skill invokes Claude Code through `claude -p` and pins the reviewer to
Claude Fable 5 (`claude-fable-5`). It does not use MCP.


Step 0: Verify prerequisites and locate the repository
------------------------------------------------------

Confirm that Claude Code is installed and authenticated:

~~~~ bash
command -v claude
claude --version
claude auth status
~~~~

Claude Fable 5 requires Claude Code 2.1.170 or later. If Claude Code is missing,
too old, unauthenticated, or Fable 5 is unavailable to the account, stop and
report the problem. Do not silently substitute another reviewer or self-review.

Note the repository root and run every Claude command from it:

~~~~ bash
REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"
~~~~

Generate a UUID for the review session and record it for every subsequent
review round.

On Unix-like systems:

~~~~ bash
SESSION_ID=$(uuidgen)
~~~~

On PowerShell:

~~~~ powershell
$SessionId = (New-Guid).Guid
~~~~

Use the generated value as `<session-id>` below. One review loop must use one
Claude session so Fable retains the context of earlier findings and fixes.


Step 1: Determine mode
----------------------

~~~~ bash
git status --short
~~~~

 -  **Pre-commit mode** — uncommitted changes exist, including untracked files
 -  **Post-commit mode** — the working tree is clean


Claude invocation rules
-----------------------

Use these options for every initial review and re-review call:

 -  `--model claude-fable-5` pins the exact reviewer model.
 -  `--effort high` gives the review a stable, thorough effort level without
    unbounded `max` spending.
 -  `--safe-mode` disables hooks, skills, plugins, auto-memory, automatically
    loaded `CLAUDE.md`, and other customizations that could interfere with or
    mutate the review environment. The prompt tells Fable to read repository
    guidance files explicitly instead.
 -  `--strict-mcp-config` prevents configured MCP servers from being loaded.
 -  `--permission-mode dontAsk` denies actions that would require interactive
    approval.
 -  `--tools "Read,Grep,Glob,Bash"` removes editing and agent-delegation tools.
 -  The `--allowedTools` rules below allow only useful read-only Git commands.

The canonical initial invocation is:

~~~~ bash
claude -p \
  --model claude-fable-5 \
  --effort high \
  --session-id "<session-id>" \
  --safe-mode \
  --strict-mcp-config \
  --permission-mode dontAsk \
  --tools "Read,Grep,Glob,Bash" \
  --allowedTools \
    "Bash(git status)" \
    "Bash(git status *)" \
    "Bash(git diff)" \
    "Bash(git diff *)" \
    "Bash(git log)" \
    "Bash(git log *)" \
    "Bash(git show)" \
    "Bash(git show *)" \
    "Bash(git branch)" \
    "Bash(git branch *)" \
    "Bash(git merge-base *)" \
    "Bash(git rev-parse *)" \
    "Bash(git ls-files *)" \
    "Bash(git ls-tree *)" \
    "Bash(git cat-file *)" \
    "Bash(git blame *)" \
    "Bash(git grep *)" \
  --disallowedTools "Edit" "Write" "NotebookEdit" "mcp__*" \
  --output-format text \
  "<review-prompt>"
~~~~

For every later round, replace `--session-id "<session-id>"` with
`--resume "<session-id>"` and retain all other options.

Capture both stdout and stderr. A nonzero exit status, authentication failure,
model-unavailable error, permission failure that prevents review, or notice
that the request was automatically rerouted to another model is **not** a clean
review. Stop and report it rather than claiming that Fable found no issues.


Pre-commit mode
---------------

### Start the Fable review session

Run the canonical initial invocation with this prompt:

~~~~
Act as an independent, read-only code reviewer.

First inspect repository guidance files such as AGENTS.md, CLAUDE.md, and
contributor documentation if they exist. Do not modify any file. Do not run
commands that change the repository or working tree, install dependencies,
access the network, or create generated artifacts.

Review all uncommitted changes in this repository, including staged, unstaged,
and untracked files. Compare them with HEAD and inspect surrounding code as
needed to understand their behavior.

Focus on actionable issues only:
- Correctness defects and behavioral regressions
- Security vulnerabilities
- Broken assumptions, error handling, and edge cases
- Concurrency, resource-lifetime, and data-integrity problems
- Material maintainability problems that are likely to cause defects
- Missing or incorrect tests when they leave changed behavior unverified

Do not report subjective style preferences, formatting nits, or speculative
concerns without a concrete failure mode.

For each issue found, state:
1. Severity
2. File and line number, when applicable
3. What the problem is
4. A concrete failure scenario
5. How to fix it

If you find no issues, respond with exactly: "No issues found."
~~~~

### Fix → re-review loop

If Fable reports issues:

1.  Verify every finding against the code. Do not apply a suggestion blindly.
2.  Apply the valid fixes yourself. Do **not** commit yet.
3.  Resume the same Claude session using the canonical invocation options and
    `--resume "<session-id>"`, with this prompt:

    ~~~~
    I've applied the valid fixes from your previous review. Please re-review
    all uncommitted changes in the repository.

    Verify that the previous issues are resolved and inspect the full current
    diff for regressions or newly introduced issues. Continue to act as a
    read-only reviewer and do not modify files or run mutating commands.

    Apply the same review criteria and reporting format as before. If everything
    looks good, respond with exactly: "No issues found."
    ~~~~

4.  Repeat until Fable's trimmed response is exactly `No issues found.`

Track whether any fixes were applied during the loop.

### Commit

Commit all changes after the review comes back clean, unless the user explicitly
asked not to commit. If any review-driven fixes were applied, add this trailer:

~~~~
Assisted-by: Claude Code:claude-fable-5
~~~~

If no fixes were needed, do not add the trailer solely because Fable reviewed
the changes.


Post-commit mode
----------------

### Determine review scope

First determine the current and default branches:

~~~~ bash
BRANCH=$(git branch --show-current)
DEFAULT_BRANCH=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')
~~~~

If `DEFAULT_BRANCH` is empty, fall back to `main` when it exists, otherwise
`master` when it exists.

 -  **Feature branch** (`BRANCH` differs from the default branch): determine the
    merge base against the local or remote default branch.

    ~~~~ bash
    BASE=$(git merge-base HEAD "$DEFAULT_BRANCH" 2>/dev/null || \
      git merge-base HEAD "origin/$DEFAULT_BRANCH")
    # Scope: $BASE..HEAD
    ~~~~

 -  **Default branch**: review commits that have not been pushed to its upstream.

    ~~~~ bash
    git log --oneline @{u}..HEAD 2>/dev/null || \
      git log --oneline "origin/$BRANCH"..HEAD 2>/dev/null
    # Scope: upstream..HEAD
    ~~~~

If the working tree is clean and there are no commits in the resulting scope,
report that there is nothing to review and stop.

### Start the Fable review session

Run the canonical initial invocation with this prompt, replacing the bracketed
text with the concrete scope you determined:

~~~~
Act as an independent, read-only code reviewer.

First inspect repository guidance files such as AGENTS.md, CLAUDE.md, and
contributor documentation if they exist. Do not modify any file. Do not run
commands that change the repository or working tree, install dependencies,
access the network, or create generated artifacts.

Review the code changes in this repository.

Review scope: [for example, "all commits in BASE..HEAD on branch feature/auth
since it diverged from main" or "the three commits on main in @{u}..HEAD"]

Inspect the complete scoped diff and surrounding code as needed.

Focus on actionable issues only:
- Correctness defects and behavioral regressions
- Security vulnerabilities
- Broken assumptions, error handling, and edge cases
- Concurrency, resource-lifetime, and data-integrity problems
- Material maintainability problems that are likely to cause defects
- Missing or incorrect tests when they leave changed behavior unverified

Do not report subjective style preferences, formatting nits, or speculative
concerns without a concrete failure mode.

For each issue found, state:
1. Severity
2. File and line number, when applicable
3. What the problem is
4. A concrete failure scenario
5. How to fix it

If you find no issues, respond with exactly: "No issues found."
~~~~

### Fix → commit → re-review loop

If Fable reports issues:

1.  Verify every finding against the code and apply the valid fixes.
2.  Commit the fix batch with a message describing what was fixed and this
    trailer:

    ~~~~
    Assisted-by: Claude Code:claude-fable-5
    ~~~~

3.  Resume the same Claude session using `--resume "<session-id>"` and all the
    canonical invocation options, with this prompt:

    ~~~~
    I've applied and committed the valid fixes from your previous review.
    Please re-review the entire original review scope, including the new fix
    commits.

    Verify that the previous issues are resolved and inspect the full current
    scoped diff for regressions or newly introduced issues. Continue to act as
    a read-only reviewer and do not modify files or run mutating commands.

    Apply the same review criteria and reporting format as before. If everything
    looks good, respond with exactly: "No issues found."
    ~~~~

4.  Repeat until Fable's trimmed response is exactly `No issues found.`

Every post-commit fix batch gets its own commit and trailer. Do not rewrite or
squash existing commits unless the user explicitly requests it.


Key rules
---------

 -  **Fable reviews; the host agent fixes.** Never ask Claude Code to edit the
    repository during this skill.
 -  **Pin the exact model.** Every call uses `--model claude-fable-5`; do not use
    the moving `fable` alias for the recorded attribution.
 -  **One Claude session per loop.** Start with `--session-id`, then use
    `--resume` with the same UUID across all review rounds.
 -  **Never paste diffs or file contents into the prompt.** Give Fable the
    repository working directory and a textual description of the scope; it
    reads the repository itself.
 -  **Read-only operation is mandatory.** Keep safe mode, strict MCP isolation,
    `dontAsk`, the restricted tool set, and the Git allowlist on every call.
 -  **Review errors are not clean reviews.** Never interpret an aborted,
    truncated, permission-blocked, fallback, or non-Fable response as
    `No issues found.`
 -  **Validate findings.** Fable's findings are advice, not ground truth; inspect
    the code before changing it.
 -  **Trailer format:** `Assisted-by: Claude Code:claude-fable-5`.
 -  **Pre-commit:** accumulate all review-driven fixes before one final commit.
 -  **Post-commit:** commit after each fix batch; every such commit gets the
    trailer.
