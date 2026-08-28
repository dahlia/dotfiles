---
name: code-review-loop
description: Finish a rough-but-working change set by running it through two independent AI reviewers — first an iterative `codex review` loop on OpenAI's frontier model, then a final `claude -p` pass on Claude's frontier model — applying only the fixes that stay inside the original goal, escalating anything that would widen the scope, and committing with the right `Assisted-by` trailers. Use this skill whenever the user asks to "run a code review loop", "review and fix my changes", "polish this before I commit", "마무리 좀 해줘", "코드 리뷰 루프", "리뷰 받고 고쳐줘", "Codex 리뷰", "Claude 리뷰", or otherwise wants a first draft brought up to committable quality by AI review — even when they name only one of the two reviewers.
---

Code Review Loop
================

Two reviewers, one scope contract.

The work is already done and roughly correct; this skill is the finishing pass
that raises it to committable quality. Codex reviews first and you iterate with
it, Claude gives the final independent read, then you commit.

The failure mode this skill exists to prevent is scope creep. Every LLM
reviewer will find *something* — that is what it is built to do. Each individual
finding looks reasonable on its own, so a loop that goes straight from
“finding” to “fix” ratchets outward until the patch has swallowed refactors,
API changes and pre-existing bugs nobody asked about. The defense is a scope
contract written *before* the first review and a triage step between every
finding and every edit.


Step 0: Write the scope contract
--------------------------------

Do this before running any reviewer. Once findings start arriving it is too
late to define scope honestly — you will rationalize each expansion as
obviously necessary.

Derive the contract from whatever established the intent: the conversation that
produced the work, the branch's commits, a linked issue or PR, the diff itself.
Write it to a scratch file so you can re-read it verbatim later in the loop,
when the temptation to widen is strongest:

~~~~ bash
REPO_ROOT=$(git rev-parse --show-toplevel)
WORK=$(mktemp -d -t review-loop.XXXXXX)   # prompts, JSON output, scope contract
SCOPE_FILE="$WORK/scope.md"
~~~~

Every file this skill generates lives in `$WORK`, never in the repository. A
prompt or a `review.json` written to the working tree shows up as an untracked
file, which dirties a clean post-commit review and can get swept into the commit
by a `git add -A` in pre-commit mode. Remove `$WORK` when the loop finishes.

Keep it to four short sections:

~~~~ markdown
## Goal
One to three sentences. What this change set is for.

## In scope
The files, modules or behaviors this change set is allowed to touch.

## Non-goals
Known problems nearby that this change set is deliberately not fixing.
Name them explicitly — an unnamed non-goal gets fixed by accident.

## Done when
The concrete condition that makes this finishable.
~~~~

State the contract back to the user in three to five lines and keep going. Do
not block on approval; the user corrects you if it is wrong. But if you cannot
tell what the change set is *for* from any available source, ask before
reviewing — a confidently wrong contract is worse than none, because it
launders scope creep as compliance.


Step 1: Determine mode and review scope
---------------------------------------

~~~~ bash
git status --short
~~~~

 -  **Pre-commit mode** — uncommitted changes exist (staged, unstaged or
    untracked). All review-driven fixes accumulate into one final commit.
 -  **Post-commit mode** — the working tree is clean. Each fix batch gets its
    own commit.

For post-commit mode, resolve the review range:

~~~~ bash
BRANCH=$(git branch --show-current)
DEFAULT_BRANCH=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')
# If empty, fall back to main, then master, whichever exists locally or on origin.
~~~~

Resolve the base to a ref that actually exists before using it. A bare `main`
is not always present — worktrees and fresh clones often have only
`origin/main`, and a branch may have no upstream at all:

~~~~ bash
# Feature branch: try the local default branch, then the remote one.
BASE=$(git merge-base HEAD "$DEFAULT_BRANCH" 2>/dev/null || \
       git merge-base HEAD "origin/$DEFAULT_BRANCH" 2>/dev/null)

# Default branch: the unpushed commits, if an upstream is configured.
UPSTREAM=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)
~~~~

 -  **Feature branch** — scope is `$BASE..HEAD`.
 -  **Default branch** — scope is `$UPSTREAM..HEAD`.

**Distinguish an empty range from a failed lookup.** If `BASE` or `UPSTREAM`
came back empty, the range did not resolve; `git log` on it will also produce
nothing, which looks identical to “no commits to review”. Reporting “nothing to
review” when a branch is in fact full of unreviewed commits is the worst
outcome this skill can produce, because it reads as a clean bill of health. Say
which ref failed to resolve and ask the user what to review against.

If the range resolved and is genuinely empty, say there is nothing to review
and stop.


Step 2: Resolve the reviewer models
-----------------------------------

Both reviewers run on the current top-tier frontier model of their vendor.
Resolve rather than hardcode, so the skill keeps working after the next release.

**Codex** — the OpenAI docs publish the current flagship in page frontmatter:

~~~~ bash
CODEX_MODEL=$(curl -sL https://developers.openai.com/api/docs/guides/latest-model.md \
  | sed -n 's/^ *model: *//p' | head -1)
~~~~

That yields an ID like `gpt-5.6-sol`. If the fetch fails, fall back to the
`model` value in `~/.codex/config.toml`, and say in your report which source you
used. `codex review` echoes `model:` in its header — confirm it matches before
trusting the output.

**Claude** — pass the moving aliases and read the exact ID back out of the
response, which is both simpler and more accurate than guessing the version
suffix. See Stage B.


Stage A: the Codex loop
-----------------------

`codex review` is one-shot; there is no session to resume, so every round is a
fresh review and you carry the continuity in the prompt.

Note two CLI constraints that shape the commands below:

 -  `--uncommitted`, `--base` and `--commit` are **mutually exclusive with a
    custom prompt**. Since the scope contract has to reach the reviewer, always
    use the custom-prompt form and describe the range in the prompt text.
 -  Pass the prompt on stdin with `-` rather than as an argument. Prompts of
    this size are awkward to quote safely.

~~~~ bash
cd "$REPO_ROOT"
codex review \
  -c model="$CODEX_MODEL" \
  -c model_reasoning_effort="high" \
  - < "$WORK/codex-prompt.txt"
~~~~

Build `$WORK/codex-prompt.txt` from the template in
`references/review-prompts.md` (section “Codex — initial review”), substituting
the range and the scope contract. Findings appear at the end of the output; the
final block is repeated once, so read it, do not count it twice.

Then, for each round:

1.  **Triage every finding** against the contract — see below. Never edit
    straight from a finding.
2.  Apply the in-scope fixes. Verify each one against the actual code first;
    findings are advice, not ground truth, and a confidently wrong one that you
    apply blindly is how correct code becomes broken code.
3.  In post-commit mode, commit the batch now (Stage C rules).
4.  Re-run `codex review` with the “Codex — re-review” template, which names the
    previous round's findings so the fresh session does not re-litigate what you
    already rejected.

**Stop when** a round returns `NO ACTIONABLE FINDINGS`, or when everything it
returns is out of scope or fails triage. Cap the loop at **three rounds** —
three rounds of new in-scope findings means the change set has a problem that
iteration is not converging on, and more rounds drift into nitpicking rather
than close the gap.

When you hit a cap with findings still open, the loop ends but the work does
not just stop. Apply the in-scope fixes from that final round, verify them
yourself, and go on to Stage B; the cap limits how many times you ask the
reviewer, not whether you fix what it found. What the cap does forbid is
silence: say plainly in your report that the last batch of fixes was applied
without a further reviewer pass, so the user knows exactly which changes carry
an independent check and which carry only yours. The same rule applies to
Stage B's two-round cap — fixes applied after Claude's second round go into the
commit unverified by Claude, and the report has to say so.


Triage: the step between finding and fix
----------------------------------------

Classify every finding from either reviewer before touching an editor:

| Class                | What it looks like                                                                      | What you do                    |
| -------------------- | --------------------------------------------------------------------------------------- | ------------------------------ |
| **In scope**         | A defect in code this change set added or modified, fixable within the contract's files | Verify, then fix               |
| **Scope-stretching** | A real defect, but the fix reaches past the contract                                    | Escalate to the user           |
| **Out of scope**     | Pre-existing or unrelated code the diff merely sits next to; already a named non-goal   | Record it, do not fix          |
| **Not actionable**   | Style preference, speculation with no failure scenario, or simply wrong about the code  | Reject, with a one-line reason |

A fix is scope-stretching when any of these hold. They are worth checking
explicitly, because in the moment each one feels like “just finishing the job”:

 -  it edits a file the change set had not already touched
 -  it changes a public API, a signature, a schema or a config format
 -  it adds or upgrades a dependency
 -  the fix is larger than the code being reviewed
 -  it is the second or third round on the same area with a smaller payoff each
    time

**When a fix would stretch scope, put the decision to the user** with
`AskUserQuestion` rather than deciding alone. Describe the defect, the failure
it causes and the size of the proper fix, then offer:

 -  **Widen the scope** — fix it properly now and accept the bigger patch.
 -  **Stopgap plus follow-up** — apply the smallest safe mitigation now and file
    a separate issue for the real fix. Usually the right default.
 -  **File it and move on** — record the issue, change nothing.

Whichever they pick, keep a running list of what was deferred and where it went.
That list is the deliverable that makes the discipline visible; without it,
“stayed in scope” is indistinguishable from “did not notice”.


Stage B: the Claude final review
--------------------------------

Codex and Claude fail differently, which is the whole point of running both.
Claude reviews last, over the code as it stands after Stage A.

Unlike Codex, Claude Code sessions resume, so one session covers all rounds and
the reviewer remembers what it already said.

Build `$WORK/claude-prompt.txt` first, from the “Claude — initial review”
template in `references/review-prompts.md`, substituting the range and the same
scope contract Stage A used. The command below redirects that file into stdin,
so if you have not written it the shell fails before `claude` ever starts.

~~~~ bash
SESSION_ID=$(uuidgen)

claude -p \
  --model fable \
  --fallback-model opus \
  --effort high \
  --session-id "$SESSION_ID" \
  --safe-mode \
  --strict-mcp-config \
  --permission-mode dontAsk \
  --output-format json \
  --tools Read Grep Glob Bash \
  --disallowedTools Edit Write NotebookEdit "mcp__*" \
    "Bash(git add *)" "Bash(git am *)" "Bash(git apply *)" \
    "Bash(git bisect *)" "Bash(git branch *)" "Bash(git checkout *)" \
    "Bash(git cherry-pick *)" "Bash(git clean *)" "Bash(git clone *)" \
    "Bash(git commit *)" "Bash(git config *)" "Bash(git fetch *)" \
    "Bash(git filter-branch *)" "Bash(git gc *)" "Bash(git init *)" \
    "Bash(git merge *)" "Bash(git mv *)" "Bash(git notes *)" \
    "Bash(git prune *)" "Bash(git pull *)" "Bash(git push *)" \
    "Bash(git rebase *)" "Bash(git reflog *)" "Bash(git remote *)" \
    "Bash(git repack *)" "Bash(git replace *)" "Bash(git reset *)" \
    "Bash(git restore *)" "Bash(git revert *)" "Bash(git rm *)" \
    "Bash(git sparse-checkout *)" "Bash(git stash *)" "Bash(git submodule *)" \
    "Bash(git switch *)" "Bash(git tag *)" "Bash(git update-ref *)" \
    "Bash(git worktree *)" \
    "Bash(git archive *)" "Bash(git bundle *)" "Bash(git checkout-index *)" \
    "Bash(git commit-tree *)" "Bash(git daemon *)" "Bash(git fast-import *)" \
    "Bash(git format-patch *)" "Bash(git hash-object *)" "Bash(git instaweb *)" \
    "Bash(git maintenance *)" "Bash(git mktree *)" "Bash(git send-email *)" \
    "Bash(git symbolic-ref *)" "Bash(git update-index *)" "Bash(git write-tree *)" \
  --allowedTools "Bash(git *)" \
  < "$WORK/claude-prompt.txt" > "$WORK/review.json"
~~~~

Why these flags: `--safe-mode` and `--strict-mcp-config` keep the reviewer out
of your hooks, skills and MCP servers, so it reads the same repository a
stranger would; `dontAsk` plus the tool set means it cannot edit the tree it is
judging. The prompt tells it to read `AGENTS.md` / `CLAUDE.md` itself, since
safe mode does not load them.

Allow `Bash(git *)` as one broad rule and subtract the mutating subcommands,
rather than allowlisting read-only ones individually. A per-subcommand
allowlist looks safer but is not: reviewers routinely run
`git -C <path> diff; git status --porcelain` as a single compound command,
which matches no `Bash(git diff *)` prefix, so the tool gets denied and the
reviewer falls back to reading files blind — a much worse review with no
security gained. Deny rules do bite on compound commands, so the mutation guard
survives. Only git is allowed; every other shell command is still refused.

The deny list has to cover every mutating subcommand, not just the obvious
write commands, because `Bash(git *)` allows anything it does not name. The
dangerous ones are the plausible-looking reads: a reviewer that runs
`git switch main` to see what the base looked like has changed the checked-out
branch, and your next fix and commit land on the wrong branch — from a command
that was, from its point of view, part of reviewing. `git branch` and `git tag`
are denied wholesale for the same reason (`-D` and `-d` delete); the reviewer
gets the branch name from `git rev-parse --abbrev-ref HEAD`, which is a pure
query. The second block covers the plumbing and file-producing commands —
`git format-patch` and `git archive -o` write files into the working tree while
looking every bit like inspection.

Treat that list as best-effort, not as the guarantee. Git has a large surface
and a deny list can only name what someone thought of; the guarantee is the
check below, which catches any mutation regardless of how it happened:

~~~~ bash
snapshot() {
  git rev-parse HEAD
  git for-each-ref --format='%(refname) %(objectname)'
  git status --porcelain -uall
  git diff HEAD
  git ls-files -o --exclude-standard -z | sort -z | xargs -0 -r sha256sum
}

BEFORE=$(snapshot)
# ... run the reviewer ...
[ "$BEFORE" = "$(snapshot)" ] || echo "REVIEWER MUTATED THE REPOSITORY"
~~~~

Compare content, not status codes. `git status --porcelain` alone reports that
a file is modified, not what is in it, so a file that was already dirty when the
review started can be rewritten underneath you and the status line never
changes. `git diff HEAD` plus hashes of the untracked files closes that gap, and
`for-each-ref` catches a moved branch or tag. The point of comparing content is
that you no longer have to reason about which git commands can mutate without
disturbing a status code — the check does not care how the change happened.

Run it around every reviewer call. If it trips, stop and show the user what
changed before doing anything else — a reviewer that altered the tree has also
invalidated its own review, and any fix you build on top of it inherits the
corruption.

**Pass the prompt on stdin.** `--tools`, `--allowedTools` and
`--disallowedTools` are all variadic, so a trailing prompt argument gets
swallowed as another tool name and the command dies with “Input must be
provided either through stdin or as a prompt argument”.

Read the result:

~~~~ bash
jq -r '.result' "$WORK/review.json"                        # the review
jq -r '.is_error, .subtype, .api_error_status' "$WORK/review.json"
jq -r '.permission_denials' "$WORK/review.json"            # blocked tools
jq -r '.modelUsage | keys[]' "$WORK/review.json" | grep -v '^claude-haiku'
~~~~

Check `permission_denials` every round, not just when something looks wrong. A
reviewer that was denied `git diff` will still produce a confident-sounding
review — of whatever it managed to read instead. That is the quietest way this
stage fails, and the output gives no sign of it.

The last one is what goes in the trailer. `modelUsage` always contains a
`claude-haiku-*` entry for Claude Code's own internal bookkeeping — ignore it.
The remaining key is the reviewer, e.g. `claude-fable-5`. If the fallback
engaged you will see `claude-opus-5` there instead; attribute the model that
actually did the work and mention the fallback in your report.

**On quota exhaustion**, `--fallback-model opus` handles routing automatically
for overload and unavailability. If the invocation itself fails with a usage
limit error, retry once with `--model opus`. If that also fails, stop and report
it. A failed, truncated or permission-blocked run is not a clean review — never
substitute your own reading of the code and present it as Claude's verdict,
because the entire value of this stage is that it is a second, independent
opinion.

For each round: triage exactly as in Stage A, apply the in-scope fixes, then —
**in post-commit mode, commit the batch before resuming** (Stage C rules), the
same as Stage A step 3. Resuming without committing means Claude re-reviews the
range `$BASE..HEAD`, which still ends at the pre-fix commit; it would grade the
code you already changed and report a stale verdict as a fresh one. Then resume
the same session with the “Claude — re-review” template.
**Replace** `--session-id "$SESSION_ID"` with `--resume "$SESSION_ID"` — do not
keep both. Claude Code rejects the
combination outright
(`--session-id can only be used with --continue or --resume if --fork-session is also specified`,
exit 1), so leaving the original flag in place means the re-review never runs
at all. Every other flag stays as it was.

**Cap at two rounds.** This is a verification pass over code Codex already went
through, so a third round is almost always drift.

Stop when the trimmed `result` is exactly `NO ACTIONABLE FINDINGS`, or when
what remains is out of scope or fails triage.


Stage C: commit
---------------

**Stage the change set first.** The `commit` skill deliberately commits only
what is already staged and does not touch the index, so anything you leave
unstaged is silently dropped from the commit — and in pre-commit mode the
review covered unstaged and untracked files too, which is exactly where a
review-driven fix tends to land. If nothing is staged, the commit fails
outright; worse, if only part of the work is staged, you get a green commit
that is missing half the fixes.

~~~~ bash
git status --short          # everything the review touched, and nothing else
git add -- <the reviewed paths>
git diff --cached --stat    # confirm this is the change set you reviewed
~~~~

Stage the reviewed paths explicitly rather than reaching for `git add -A`. A
blanket add sweeps in whatever else happens to be in the tree — build output,
editor scratch files, an unrelated experiment — none of which any reviewer
looked at. `$WORK` lives outside the repository precisely so it cannot be caught
this way, but the rest of the tree is still yours to be careful with.

Leave `$WORK` in place here. In post-commit mode this stage runs after every fix
batch, and the next review round still needs the scope contract and the prompt
files inside it; deleting the directory mid-loop breaks the very next
redirection. `$WORK` is cleaned up once, at the end of Step 3.

Then follow the `commit` skill for the message itself.

Add an `Assisted-by` trailer for each reviewer whose findings actually changed
the code — attribution is a record of what shaped the commit, so a reviewer that
found nothing does not get a trailer merely for having run:

~~~~
Assisted-by: Codex:gpt-5.6-sol
Assisted-by: Claude Code:claude-fable-5
~~~~

Substitute the resolved IDs from Step 2 and Stage B. Codex first, since it
reviewed first.

 -  **Pre-commit mode** — one commit at the end, carrying whichever trailers
    apply.
 -  **Post-commit mode** — commit after each fix batch, with that reviewer's
    trailer on that commit. Do not rewrite or squash existing commits unless the
    user asks.


Step 3: Report
--------------

Close with a short summary the user can act on:

 -  What each reviewer found, and how many rounds each took.
 -  What you fixed.
 -  What you rejected, and why (one line each).
 -  **What was deferred**, with the issue it went to and the stopgap you left
    behind, if any.
 -  Which models ran, including any fallback.
 -  Any fixes applied after a round cap, which therefore carry no independent
    reviewer pass.

Then remove the scratch directory — this is the only place it gets deleted, and
only once every round and every commit is behind you:

~~~~ bash
rm -rf "$WORK"
~~~~

The deferred list matters most. It is the evidence that the loop tightened the
change set instead of expanding it.


Key rules
---------

 -  **The contract comes first.** No reviewer runs before it is written, and
    every finding is measured against it.
 -  **Reviewers review; you fix.** Both are configured read-only. Never let
    either edit the repository.
 -  **Never paste diffs into a prompt.** Both reviewers read the repository
    themselves; give them the range and the contract.
 -  **Verify before applying.** A finding is a hypothesis about the code, and
    plausible-sounding ones are wrong often enough to matter.
 -  **Round caps are real.** Three Codex rounds, two Claude rounds. Past that,
    report instead of looping — the marginal finding stops being worth the
    marginal risk.
 -  **Keep every generated file in `$WORK`.** Prompts and JSON output written
    into the repository dirty a clean tree and can be swept into the commit.
 -  **Snapshot the repository around every reviewer call.** The deny list is
    best-effort; the content comparison is what actually guarantees the reviewer
    changed nothing.
 -  **Stage explicitly before committing.** The `commit` skill commits only what
    is already staged, and pre-commit reviews cover unstaged and untracked
    files.
 -  **A failed review is not a clean review.** Errors, truncation, permission
    blocks and unexpected model substitutions all mean “unknown”, never “no
    issues found”.
 -  **Escalate, do not expand.** Widening scope is the user's call, and it is
    cheap to ask.
