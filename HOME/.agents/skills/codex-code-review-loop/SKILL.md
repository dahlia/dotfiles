---
name: codex-code-review-loop
description: Run an iterative code review loop using the Codex MCP — Codex reviews the code and you apply fixes, repeating until Codex finds nothing more to fix. Use this skill whenever the user asks to "run a Codex review", "do a Codex code review loop", "let Codex review my code", "코드 리뷰 루프", "Codex로 코드 리뷰", or wants to review and iteratively fix uncommitted changes or a branch using the Codex AI agent.
---

Codex Code Review Loop
======================

Codex reviews; you fix. Repeat until the review comes back clean.


Step 0: Determine execution context
-----------------------------------

Check whether `mcp__codex__codex` is available in your tool list.

 -  **Available** → you are running inside Claude Code. Follow the **External
    Codex review** path below.
 -  **Not available** → you are running inside Codex itself. Follow the
    **Self-review** path below.

Also note the repository root:

~~~~ bash
git rev-parse --show-toplevel
~~~~


Step 1: Determine mode
----------------------

~~~~ bash
git status --short
~~~~

 -  **Pre-commit mode** — uncommitted changes exist (staged or unstaged)
 -  **Post-commit mode** — working tree is clean


External Codex review path (running in Claude Code)
---------------------------------------------------

### Look up the current frontier model

Fetch `https://developers.openai.com/api/docs/guides/latest-model.md` and
extract the exact model ID of the current frontier model. Note it — you'll use
it for every Codex call and for the `Assisted-by` trailer.

### Pre-commit mode

**Start the Codex review session** — call `mcp__codex__codex`:

| Parameter | Value                                |
| --------- | ------------------------------------ |
| `model`   | the frontier model ID from above     |
| `cwd`     | absolute path to the repository root |
| `sandbox` | `"read-only"`                        |
| `prompt`  | see below                            |

~~~~
Please review the uncommitted changes in this repository (both staged and unstaged).

Focus on:
- Correctness and potential bugs
- Security vulnerabilities
- Code quality and maintainability
- Edge cases

For each issue found, state:
1. File and line number (if applicable)
2. What the problem is
3. How to fix it

If you find no issues, respond with exactly: "No issues found."
~~~~

Record the `threadId` from the response.

**Fix → re-review loop** — if issues were found:

1.  Apply the fixes yourself (do **not** commit yet)
2.  Continue the same Codex session via `mcp__codex__codex-reply`:
     -  `threadId`: the thread ID recorded above

     -  `prompt`:

        ~~~~
        I've applied the fixes you suggested. Please re-review the uncommitted changes.
        Check whether the previous issues are resolved and look for any new issues.
        If everything looks good, respond with exactly: "No issues found."
        ~~~~
3.  Repeat until Codex responds with “No issues found.”

Track whether any fixes were applied during the loop.

**Commit** — commit all changes. If any fixes were applied, add this trailer
(substituting the actual model ID):

~~~~
Assisted-by: Codex:gpt-5.5
~~~~

### Post-commit mode

**Determine review scope:**

~~~~ bash
BRANCH=$(git branch --show-current)
~~~~

 -  **Feature branch** (not main/master):

    ~~~~ bash
    BASE=$(git merge-base HEAD main 2>/dev/null || git merge-base HEAD master 2>/dev/null)
    # Scope: commits from $BASE to HEAD
    ~~~~

 -  **On main/master**:

    ~~~~ bash
    git log --oneline @{u}..HEAD 2>/dev/null || git log --oneline origin/$(git branch --show-current)..HEAD 2>/dev/null
    # Scope: commits since last upstream push
    ~~~~

**Start the Codex review session** — call `mcp__codex__codex` with the same
parameters as above, but use this prompt (fill in the scope before sending):

~~~~
Please review the code changes in this repository.

Review scope: [e.g. "the commits on branch 'feature/auth' since it diverged from main" or "the 3 commits on main since the last upstream push"]

Focus on:
- Correctness and potential bugs
- Security vulnerabilities
- Code quality and maintainability
- Edge cases

For each issue found, state:
1. File and line number (if applicable)
2. What the problem is
3. How to fix it

If you find no issues, respond with exactly: "No issues found."
~~~~

**Fix → commit → re-review loop** — if issues were found:

1.  Apply the fixes

2.  Commit with a message describing what was fixed, plus the trailer:

    ~~~~
    Assisted-by: Codex:<model-id>
    ~~~~

3.  Continue the Codex session via `mcp__codex__codex-reply` with the same
    re-review prompt as above

4.  Repeat until Codex responds with “No issues found.”


Self-review path (running inside Codex)
---------------------------------------

When you are Codex, review the code yourself — no external MCP call needed.

### Pre-commit mode

1.  Read the uncommitted changes: `git diff HEAD` (or `git diff --cached` for
    staged-only)

2.  Review the code yourself, applying the same criteria: correctness,
    security, quality, edge cases

3.  If you find issues, fix them directly (do **not** commit yet), then re-read
    the diff and review again

4.  Repeat until your own review finds no further issues

5.  Commit all changes. If any fixes were applied, include the trailer using
    your own model ID:

    ~~~~
    Assisted-by: Codex:<your-model-id>
    ~~~~

### Post-commit mode

Determine review scope the same way as in the external path above.

1.  Read the relevant commits: `git diff <base>..HEAD` or `git show` as
    appropriate

2.  Review the code yourself

3.  If you find issues, fix and commit them with the trailer:

    ~~~~
    Assisted-by: Codex:<your-model-id>
    ~~~~

4.  Re-read the full diff and review again

5.  Repeat until your review finds no further issues


Key rules
---------

 -  **Never paste diffs or file contents into the Codex prompt** (external
    path). Pass only `cwd` and a description of the scope — Codex reads the
    repository itself.
 -  **One Codex thread per session** (external path). Reuse the same `threadId`
    across all review rounds so Codex retains context about what has already
    been reviewed and fixed.
 -  **`Assisted-by` trailer format**: `Assisted-by: Codex:<exact-model-id>` —
    use the model ID exactly as it appears (e.g. `Assisted-by: Codex:gpt-5.5`).
 -  **Pre-commit**: accumulate all fixes before committing; the trailer goes on
    that single commit.
 -  **Post-commit**: commit after each fix batch; every such commit gets the
    trailer.
