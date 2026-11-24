Review evidence from a disposable copy
======================================

Use this with Step 3 of [the skill](../SKILL.md). The origin report protects
the user's work; the copy report identifies evidence that may be stale.
Neither before/after comparison tells you when a command ran relative to a
write. An unchanged end state does not prove a file was never changed and
restored; take observed or reported temporary changes into account too.

Evidence and confirmation
-------------------------

1. Verify termination and an unchanged origin first. Then inspect the copy
   guard's changed paths, answer, and available tool trace. In the absence
   of a trace or other corroboration, do not infer order from a reviewer's
   assurance or a clean sentinel.
2. Keep findings you can verify against the baseline. For example, a test
   added in scratch that fails against unchanged implementation can support
   a finding. A test that passes only after the reviewer edits implementation,
   fixtures or configuration cannot establish that the original works. Treat
   unidentified inputs and timing as unverified. Triage confirmed findings
   against the existing scope contract. Write any authorized fixes yourself;
   never copy a reviewer's edits into the real checkout.
3. Count the round as usable if its ordinary completion, model, permission
   and coverage checks pass **and** the necessary reads and checks are shown
   to apply to the baseline. A protected but irrelevant output written after
   inspection need not invalidate any coverage. If evidence for any required
   portion is missing, keep the initial result and run at most one
   confirmation attempt for this round, before applying fixes. Use the same
   route and baseline, a new copy, task/session and prefix, and checks that
   will not alter their own inputs. Ask for only the missing coverage if
   the rest is established; ask for a full fresh review if you cannot tell
   what was affected. The coordinator's own verification can establish a
   finding, but cannot replace missing independent review coverage.
4. If coverage still cannot be established, mark the step `unknown`, apply
   only verified in-scope fixes under the normal commit rules, and run the
   remaining independent steps. A clean sentinel from the confirmation
   attempt can cover only the scope actually sent to it. Do not retry the
   confirmation itself. Honor round caps; if no confirmation fits, report
   the gap. Never treat validation on reviewer-edited inputs as a clean
   result.

For a confirmation prompt, use the normal role, scope and read-only rules,
followed by the specific gaps (or a request to review the entire baseline).
Do not present an earlier clean verdict as evidence. Prefer static inspection
or checks with stdout and scratch-only outputs over repeating a command
that rewrote protected inputs.

Keep reports and output for any incident. After verified termination and
classification, unchanged successful copies can be removed to reclaim
space; keep mutated or uncertain copies private under `$WORK` for diagnosis.

Unsupported checkouts and hosts
-------------------------------

The copier rejects checkouts that cannot be copied directly without following
external Git metadata (e.g. linked worktrees, absorbed submodules, shared
object stores) and symlinks whose targets would change when copied. Never
substitute a lossy clone or `git worktree`. If the original changed during
preparation, preserve the partial artifacts and stop.

For an unsupported layout, use a **protected inspection-only** CLI pass if
available. Use a fresh prefix and the same guarded CLI invocation with
`--root "$REPO_ROOT"` and `--cwd "$REPO_ROOT"`, but omit `--prepare-copy` and
`--verify-origin`; `guard.json` now guards the original and any mutation or
failure in it is a hard stop. Set `[OUTPUT PATHS]` to none. Create
`"$WORK/tmp"` and export `CROSS_SUBAGENTS_CACHE_DIR="$WORK/cross-cache"` for
this invocation. Replace the `bwrap` line with:

~~~~ bash
bwrap --ro-bind / / --bind "$WORK" "$WORK" --dev /dev --proc /proc \
  --unshare-pid --die-with-parent --setenv TMPDIR "$WORK/tmp" -- \
~~~~

Keep `$WORK` outside the original checkout and its Git storage. This mount
also protects shared metadata outside the checkout. Do not add writable
exceptions for the CLI's home or repository just to make startup succeed.
Tell the reviewer to use static inspection or checks with scratch-only
outputs. Report checks that could not run; never call a blocked pass clean.
If no protected execution is available, report the route unavailable and
continue with the others where possible. Never fall back to unprotected
execution against the original.
