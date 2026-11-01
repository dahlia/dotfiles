Review prompt templates
=======================

Four templates: the fast filter and the frontier reviewer, each with an initial
and a re-review version. They are agent-neutral because `cross-subagents` runs
whichever route the plan selected. Substitute the bracketed placeholders before
sending.

 -  `[RANGE]` — a plain-English description of what to review, e.g.
    “all uncommitted changes, including staged, unstaged and untracked files”,
    or “the commits in `a1b2c3d..HEAD` on branch `feature/auth`“.
 -  `[GOAL]`, `[IN SCOPE]`, `[NON-GOALS]` — copied verbatim from the scope
    contract written in Step 0.
 -  `[OUTPUT PATHS]` — declared disposable repository output directories, or
    none.
 -  `[PREVIOUS FINDINGS]` — for re-reviews, a terse list of the previous round's
    findings, each marked *fixed*, *rejected* (with the reason) or *deferred*.
 -  `[ROLE]` — for step 2, `second step`; for step 3, `independent final read`.
 -  `[PRIOR STEP SUMMARY]` — what earlier steps reported and what was applied.

The scope block is not decoration. Telling the reviewer what the change set is
for suppresses most out-of-scope findings at the source, which is far cheaper
than triaging them away afterwards.

Contents:

 -  [Fast filter — initial review](#fast-filter--initial-review)
 -  [Fast filter — re-review](#fast-filter--re-review)
 -  [Frontier — initial review](#frontier--initial-review)
 -  [Frontier — re-review](#frontier--re-review)


Fast filter — initial review
----------------------------

Step 1. The reviewer can inspect code and run focused checks. Only the final
assistant message is classified, so put the complete review there.

~~~~
Act as a code reviewer doing a fast first pass.

Review scope: [RANGE]

Read the repository yourself using file tools and shell inspection commands.
Do not edit source, fixtures, manifests, lockfiles or Git state. You may run
focused tests, builds and type checks with existing dependencies. Temporary
files and runtime caches are allowed; repository outputs must stay in these
declared disposable paths: [OUTPUT PATHS]. Do not install dependencies, update
snapshots, regenerate protected source or start/change external services. If a
necessary read or check is blocked, report the limitation instead of guessing.
The author backs up and checks protected inputs after each attempt.
Also read AGENTS.md, CLAUDE.md and any contributor documentation, and hold
the change set to the conventions they state.

This change set has a deliberately bounded goal:

Goal: [GOAL]
In scope: [IN SCOPE]
Explicit non-goals: [NON-GOALS]

Two stronger reviewers will read this code after you. Your job is to catch
the clear, concrete defects first: wrong logic, off-by-one errors, wrong
conditions, unhandled errors, crashes, broken edge cases, typos in
identifiers, and changed behavior with no test.

Report a finding only when all three hold:

1. It is a defect in code this change set added or modified — not in
   pre-existing code the diff merely sits next to.
2. You can state a concrete failure: specific inputs or state that lead to a
   wrong result, a crash, corrupted or lost data, or a security hole.
3. Fixing it does not require work listed under the non-goals.

Do not report style or formatting preferences, naming opinions, speculation
with no failure scenario, or refactors that do not fix a defect. Report
defects outside the goal separately, under a heading "OUT OF SCOPE".

For each finding give: severity, file and line, what breaks, the concrete
failure scenario, and the smallest fix that resolves it.

Put the complete review in your final message. If you could not read
something the review needed, say so there rather than guessing.

If nothing meets the bar, your final message must be exactly:
NO ACTIONABLE FINDINGS
~~~~


Fast filter — re-review
-----------------------

Resume the step 1 session, so the reviewer remembers its own findings. A
transient retry starts a fresh session, so this template repeats the scope.

~~~~
I have applied the valid fixes from your review. Findings I did not act on:

[PREVIOUS FINDINGS]

Re-review the same scope: [RANGE]

Read AGENTS.md, CLAUDE.md and contributor documentation yourself.
Goal: [GOAL]
In scope: [IN SCOPE]
Explicit non-goals: [NON-GOALS]

Report only defects introduced by the scoped changes with a concrete failure
scenario. Reject style preferences and speculative refactors. For each finding,
give severity, file and line, what breaks, the failure scenario and the smallest
fix. Put serious unrelated defects under "OUT OF SCOPE".
Do not edit source, fixtures, manifests, lockfiles or Git state. Focused tests,
builds and type checks using existing dependencies may produce temporary files
and caches. Repository outputs must stay in [OUTPUT PATHS]. Do not install
dependencies, update snapshots, regenerate protected source or start/change
external services. Report necessary reads or checks that could not run.

Confirm the fixes are genuinely resolved and introduced no regressions, then
check the current state for anything new. Use the same inspection and test
policy as before. Do not re-raise findings marked rejected or deferred above.
Apply the same bar and the same reporting format, including the separate
"OUT OF SCOPE" heading, and put the complete review in your final message.

If nothing meets the bar, your final message must be exactly:
NO ACTIONABLE FINDINGS
~~~~


Frontier — initial review
-------------------------

Steps 2 and 3. Set `[ROLE]` and `[PRIOR STEP SUMMARY]` so the reviewer knows
whether it is the second step or the independent final read.

~~~~
Act as an independent code reviewer. This is the [ROLE] in a three-step loop.

Start by reading AGENTS.md, CLAUDE.md and any contributor documentation in this
repository, and hold the change set to the conventions they state. You are
running isolated, so none of that is loaded for you automatically.

Do not edit source, fixtures, manifests, lockfiles or Git state. You may run
focused tests, builds and type checks using existing dependencies. Temporary
files and runtime caches are allowed; repository outputs must stay in these
declared disposable paths: [OUTPUT PATHS]. Do not install dependencies, update
snapshots, regenerate protected source or start/change external services.
Your shell may not be sandboxed. The author backs up and checks protected
inputs, but that does not prevent writes outside the repository. Report any
necessary read or check that could not run; do not claim a complete clean
review while required evidence is missing.

Review scope: [RANGE]

Inspect the scoped diff and read enough of the surrounding code to judge
whether the change is correct in context.

This change set has a deliberately bounded goal, and findings that push past it
cost the author more than they return:

Goal: [GOAL]
In scope: [IN SCOPE]
Explicit non-goals: [NON-GOALS]

Earlier steps in this loop reported:

[PRIOR STEP SUMMARY]

Assume the obvious defects are gone and look for what a first pass misses:
incorrect assumptions about how the surrounding system behaves, error and
failure paths that were never exercised, concurrency and resource-lifetime
problems, data-integrity and migration hazards, security weaknesses, and
behavior that contradicts the repository's documented conventions.

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


Frontier — re-review
--------------------

Resume the same step session. The reviewer still has its own previous findings
in context, so the prompt can stay short and repeat only the decisions.

~~~~
I have applied the valid fixes from your review. Findings I did not act on:

[PREVIOUS FINDINGS]

Please re-review the same scope. Confirm the fixes are genuinely resolved and
that they introduced no regressions, then check the current state for anything
new.

Keep the same inspection and test policy. Do not edit protected inputs or Git
state. Allowed repository output paths: [OUTPUT PATHS]. Report blocked checks.
Do not re-raise findings marked rejected or deferred above. Apply the same bar
and the same reporting format as before, including the separate
"OUT OF SCOPE" heading.

If nothing meets the bar, respond with exactly: NO ACTIONABLE FINDINGS
~~~~
