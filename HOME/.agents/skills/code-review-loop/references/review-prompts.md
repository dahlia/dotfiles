Review prompt templates
=======================

Four templates: an initial and a re-review prompt for each reviewer. Substitute
the bracketed placeholders before sending.

 -  `[RANGE]` — a plain-English description of what to review, e.g.
    “all uncommitted changes, including staged, unstaged and untracked files”,
    or “the commits in `a1b2c3d..HEAD` on branch `feature/auth`“.
 -  `[GOAL]`, `[IN SCOPE]`, `[NON-GOALS]` — copied verbatim from the scope
    contract written in Step 0.
 -  `[PREVIOUS FINDINGS]` — for re-reviews, a terse list of the previous round's
    findings, each marked *fixed*, *rejected* (with the reason) or *deferred*.

The scope block is not decoration. Telling the reviewer what the change set is
for suppresses most out-of-scope findings at the source, which is far cheaper
than triaging them away afterwards.

Contents:

 -  [Codex — initial review](#codex--initial-review)
 -  [Codex — re-review](#codex--re-review)
 -  [Claude — initial review](#claude--initial-review)
 -  [Claude — re-review](#claude--re-review)


Codex — initial review
----------------------

~~~~
Review scope: [RANGE]

Read the repository yourself. Also read AGENTS.md, CLAUDE.md and any
contributor documentation, and hold the change set to the conventions they
state.

This change set has a deliberately bounded goal. Work outside it belongs to
someone else, and reporting it spends the author's attention on decisions they
have already made:

Goal: [GOAL]
In scope: [IN SCOPE]
Explicit non-goals: [NON-GOALS]

Report a finding only when all three hold:

1. It is a defect in code this change set added or modified — not in
   pre-existing code the diff merely sits next to.
2. You can state a concrete failure: specific inputs or state that lead to a
   wrong result, a crash, corrupted or lost data, or a security hole.
3. Fixing it does not require work listed under the non-goals.

Missing tests count as a finding when they leave changed behavior unverified
and you can name the specific case that would go undetected.

Report separately, under a heading "OUT OF SCOPE", any defect serious enough
that the author should know about it even though it falls outside the goal.
Keep these out of the main findings list so they can be triaged as a batch.

Do not report style or formatting preferences, naming opinions, speculation
with no failure scenario, or refactors that do not fix a defect. Do not propose
broadening the change set's design.

For each finding give: severity, file and line, what breaks, the concrete
failure scenario, and the smallest fix that resolves it. Prefer the fix that
touches the fewest files.

If nothing meets the bar, respond with exactly: NO ACTIONABLE FINDINGS
~~~~


Codex — re-review
-----------------

`codex review` starts a fresh session each time, so this prompt carries the
continuity. Naming the rejected findings is what stops the loop from
re-proposing them round after round.

~~~~
Review scope: [RANGE]

This is a follow-up review. In the previous round you raised:

[PREVIOUS FINDINGS]

Confirm that the fixed items are genuinely resolved and that the fixes
introduced no regressions, then review the current state of the scope for
anything new.

Do not re-raise findings marked rejected or deferred above; those decisions are
made. If you believe a rejection was factually mistaken, say so once in a
single line and move on.

The goal, scope and non-goals are unchanged:

Goal: [GOAL]
In scope: [IN SCOPE]
Explicit non-goals: [NON-GOALS]

Apply the same bar as before: a defect in code this change set added or
modified, with a concrete failure scenario, fixable without doing the
non-goals. Same reporting format, and the same separate "OUT OF SCOPE" heading.

If nothing meets the bar, respond with exactly: NO ACTIONABLE FINDINGS
~~~~


Claude — initial review
-----------------------

Claude reviews after the Codex loop has settled, so this prompt asks for an
independent read rather than a confirmation of what Codex already covered.

~~~~
Act as an independent, read-only code reviewer.

Start by reading AGENTS.md, CLAUDE.md and any contributor documentation in this
repository, and hold the change set to the conventions they state. You are
running in safe mode, so none of that is loaded for you automatically.

Do not modify any file. Do not run commands that change the repository or
working tree, install dependencies, reach the network, or produce generated
artifacts.

Review scope: [RANGE]

Inspect the scoped diff and read enough of the surrounding code to judge
whether the change is correct in context.

This change set has a deliberately bounded goal, and findings that push past it
cost the author more than they return:

Goal: [GOAL]
In scope: [IN SCOPE]
Explicit non-goals: [NON-GOALS]

Another frontier model has already reviewed this code and its findings have
been applied. Assume the obvious defects are gone and look for what a first
pass misses: incorrect assumptions about how the surrounding system behaves,
error and failure paths that were never exercised, concurrency and
resource-lifetime problems, data-integrity and migration hazards, security
weaknesses, and behavior that contradicts the repository's documented
conventions.

Report a finding only when all three hold:

1. It is a defect in code this change set added or modified — not in
   pre-existing code the diff merely sits next to.
2. You can state a concrete failure: specific inputs or state that lead to a
   wrong result, a crash, corrupted or lost data, or a security hole.
3. Fixing it does not require work listed under the non-goals.

Missing tests count as a finding when they leave changed behavior unverified
and you can name the specific case that would go undetected.

Report separately, under a heading "OUT OF SCOPE", any defect serious enough
that the author should know about it even though it falls outside the goal.

Do not report style or formatting preferences, naming opinions, speculation
with no failure scenario, or refactors that do not fix a defect.

For each finding give: severity, file and line, what breaks, the concrete
failure scenario, and the smallest fix that resolves it. Prefer the fix that
touches the fewest files.

If nothing meets the bar, respond with exactly: NO ACTIONABLE FINDINGS
~~~~


Claude — re-review
------------------

Sent with `--resume "$SESSION_ID"`, so the reviewer still has its own previous
findings in context and the prompt can stay short.

~~~~
I have applied the valid fixes from your review. Findings I did not act on:

[PREVIOUS FINDINGS]

Please re-review the same scope. Confirm the fixes are genuinely resolved and
that they introduced no regressions, then check the current state for anything
new.

Stay read-only: do not modify files or run mutating commands. Do not re-raise
findings marked rejected or deferred above. Apply the same bar and the same
reporting format as before, including the separate "OUT OF SCOPE" heading.

If nothing meets the bar, respond with exactly: NO ACTIONABLE FINDINGS
~~~~
