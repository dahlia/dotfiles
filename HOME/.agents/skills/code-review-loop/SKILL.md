---
name: code-review-loop
description: Finish a rough-but-working change set by running it through independent AI reviewers — a cheap read-only first pass through OpenCode on DeepSeek Flash when OpenCode is available, then an iterative `codex review` loop on OpenAI's frontier model, then a final `claude -p` pass on Claude's frontier model — applying only the fixes that stay inside the original goal, escalating anything that would widen the scope, and committing with the right `Assisted-by` trailers. Use this skill whenever the user asks to "run a code review loop", "review and fix my changes", "polish this before I commit", "마무리 좀 해줘", "코드 리뷰 루프", "리뷰 받고 고쳐줘", "Codex 리뷰", "Claude 리뷰", "OpenCode 리뷰", or otherwise wants a first draft brought up to committable quality by AI review — even when they name only one of the reviewers.
---

Code Review Loop
================

Three reviewers, one scope contract.

The work is already done and roughly correct; this skill is the finishing pass
that raises it to committable quality. When OpenCode is installed, a cheap
DeepSeek Flash pass catches the obvious defects first. Codex then reviews and
you iterate with it, Claude gives the final independent read, then you commit.

The cheap pass exists to protect quota, not to replace anyone. ChatGPT and
Claude subscriptions run out of weekly quota, and a Codex round spent on an
off-by-one that a small model would have caught is a round not spent on the
problems only a frontier model finds. The Codex loop and the Claude pass run in
full whatever the first pass reports.

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

Codex and Claude run on the current top-tier frontier model of their vendor.
Resolve rather than hardcode, so the skill keeps working after the next release.

**OpenCode** — the one exception: the first pass is deliberately *not* a
frontier model, and its model list is fixed in a deterministic preference
order. See the pre-stage below.

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


Pre-stage: the OpenCode first pass
----------------------------------

A small, cheap model reads the change set before Codex does. Its job is to
catch the defects that are obvious once someone looks (wrong conditions,
off-by-one errors, unhandled errors, typos in identifiers) so that Codex and
Claude spend their rounds on the rest. It is advice from a weaker reviewer:
every finding goes through triage like any other, and nothing about this stage
shortens, skips or replaces Stage A or Stage B.

All of the mechanics live in `scripts/opencode-review.sh` next to this file.
Use the script rather than retyping its commands: the read-only guard, the
repository snapshot and the result checks only hold if every round runs them
the same way, and shell state does not survive between separate tool calls.

~~~~ bash
OC_REVIEW="<this skill's directory>/scripts/opencode-review.sh"
bash "$OC_REVIEW" models
~~~~

### Availability and model preference

`models` prints the usable models, one per line, in this fixed order:

1.  `deepseek/deepseek-flash` — DeepSeek's own API, under its canonical
    model ID.
2.  `opencode-go/deepseek-v4.1-flash` — the same model through the OpenCode Go
    subscription. OpenCode Go's catalog has no `deepseek-flash` entry, so the
    call has to use this ID even though DeepSeek no longer uses versioned IDs.

DeepSeek's first-party API comes first because it is one hop with no proxy in
between. OpenCode Go has had bugs of its own with DeepSeek models (dropped
tool-call names, a second turn failing with a missing-field 400), and that is
exactly the path a multi-round, tool-heavy review exercises. Both passed a
resumed two-round review with tool calls on OpenCode 1.18.31, so Go is a real
fallback, not a placeholder. Never add a legacy alias such as
`deepseek-v4-flash` to the list: DeepSeek routes old IDs to whatever is
current, so a pinned legacy ID records a model that did not run.

If `opencode` is not installed, or neither model is configured, `models` exits
3 with a one-line reason on stderr. Say “OpenCode pre-stage skipped: <reason>”
in one line and go straight to Stage A. That is the *unavailable* outcome, and
it is not a failure of anything.

### Running a round

Build `$WORK/opencode-prompt.txt` from the “OpenCode — initial review” template
in `references/review-prompts.md`, with the same range and scope contract the
other stages use. As with them, never paste a diff or file contents into it;
the reviewer reads the repository itself. Then run round 1 on the first model
`models` printed:

~~~~ bash
REPO_ROOT="$REPO_ROOT" WORK="$WORK" bash "$OC_REVIEW" run "$OC_MODEL" 1
~~~~

The script prints `STATUS=<status> MODEL=<id> SESSION=<id>`, then any `REASON=`
and `DENIED=` lines, and saves the final answer to
`$WORK/opencode-r1.review.txt` (`r2` for round 2). Its exit code is the status:

| Exit | Status        | Meaning                                                   | What you do                                    |
| ---- | ------------- | --------------------------------------------------------- | ---------------------------------------------- |
| 0    | `clean`       | The final answer is exactly `NO ACTIONABLE FINDINGS`      | End the stage                                  |
| 10   | `findings`    | A complete review with findings                           | Triage, fix, re-review (below)                 |
| 20   | `failed`      | The reviewer ran, but the round is not a usable review    | End the stage; report it as failed             |
| 21   | `blocked`     | A complete answer, but a tool call was denied             | As `findings`, but never count it as clean     |
| 30   | `unavailable` | The provider returned nothing (401, unknown model, quota) | Round 1: next model. Later rounds: as `failed` |
| 40   | `mutated`     | The repository changed while the reviewer ran             | Stop and show the user, as for any reviewer    |

On round 1, `unavailable` moves on to the next model in the list, starting a
fresh session. If every model comes back `unavailable`, the stage is skipped as
unavailable, and the report lists each model's `REASON=` lines. Once a model has
served round 1, it is the stage's model: record `MODEL` and `SESSION` from the
status line and use both for the next round. Never switch models mid-stage;
a different model resuming the session is a different reviewer.

`failed` covers everything that looks like an answer but is not one. The script
treats each of these as a failure:

 -  `opencode run` exits non-zero, or `timeout` stops it (`OC_TIMEOUT`,
    default 900 seconds)
 -  an `error` event in the JSON stream, or an error on an assistant message,
    such as `APIError 401`
 -  no assistant message in the round
 -  any assistant message whose `providerID/modelID`, variant or agent is not
    the one requested
 -  a final step that finished with anything but `stop`, such as `length`
 -  an empty final text: DeepSeek models through OpenCode sometimes finish
    their reasoning and return no answer at all, and an empty reply must never
    read as “nothing to report”

A clean result needs the exact sentinel, so silence can never pass for one.
`blocked` exists because a reviewer that was refused a read still writes a
confident review of whatever it managed to see — the same quiet failure Stage B
guards against with `permission_denials`. Its findings are still worth triaging,
but a `blocked` round that says `NO ACTIONABLE FINDINGS` is unknown, not clean.

### Re-review

For each round: triage every finding against the contract, verify each against
the code, and apply only the in-scope fixes, exactly as in Stage A. In
post-commit mode, commit the batch before resuming (Stage C rules), for the same
stale-range reason Stage B gives. Then rewrite `$WORK/opencode-prompt.txt` from
the “OpenCode — re-review” template and resume the same session:

~~~~ bash
REPO_ROOT="$REPO_ROOT" WORK="$WORK" \
  bash "$OC_REVIEW" run "$OC_MODEL" 2 "$OC_SESSION"
~~~~

**Cap at two rounds**: one review and one re-review of the fixes. This stage is
a filter in front of reviewers that will read the same code again, so a third
round buys little, and a small model asked to keep looking drifts into
nitpicks. When round 2 still returns in-scope findings, verify and apply them,
then go on to Stage A; Codex reviews those fixes as part of the range, so they
do not stay unverified the way post-cap fixes elsewhere do. Say in the report
that the stage ended at its cap.

When the stage ends as `failed`, `mutated`, or `unavailable` on every model, go
on to Stage A all the same (after the user has seen a mutation). Never
substitute your own reading and report it as OpenCode's verdict.

### How the script keeps the reviewer read-only

The guard is enforced by OpenCode's permission system, not by the prompt:

 -  **A dedicated agent**, `review-loop-flash`, passed through
    `OPENCODE_CONFIG_CONTENT` and pinned with `--agent`. Its permissions start
    from `"*": "deny"`; OpenCode applies the last matching rule, so everything
    not allowed afterwards stays denied, including `edit`, `write`, `task`,
    `webfetch`, `websearch`, `skill`, `lsp` and `question`. OpenCode removes
    tools denied this way from the model's tool list altogether.
 -  **Reads stay in the repository.** `read`, `glob`, `grep` and `list` are
    allowed, `.env` files are denied, and `external_directory` is denied except
    for OpenCode's own `tool-output` directory, where it stores long command
    output for the reviewer to page through.
 -  **Bash is an allowlist of read-only git commands** (`git status`, `git
    diff`, `git log`, `git show`, `git blame`, `git grep`, and a few plumbing
    queries), plus deny rules for `>` redirection, `--output`, `--no-index`,
    `--contents` and `git grep -O`. This is the opposite of Stage B's deny list,
    for a reason: OpenCode parses a compound command, pipeline or command
    substitution into its separate commands and checks each one, including its
    redirections, so an allowlist does not break on `git diff; git status` the
    way a Claude Code prefix rule does. `git -C`, `git -c`, `cd`, environment
    assignments and every non-git command match no allow rule and are refused.
 -  **No outside configuration.** `OPENCODE_DISABLE_PROJECT_CONFIG` ignores the
    reviewed repository's own *opencode.json* and *.opencode/*, `--pure` loads
    no plugins, Claude Code prompts and external skills are off, sharing and
    auto-update are off, and every MCP server in the user's global config is
    disabled by name, so the reviewer has no GitHub, browser or other tools.
 -  **A preflight check.** Before each round the script asks
    `opencode debug agent` for the resolved agent and refuses to run unless the
    catch-all rule is `deny` and the edit, write, task and web tools are off. An
    unknown or shadowed agent would otherwise run with default permissions.
 -  **The snapshot check** from Stage B runs around every round, and a change
    ends the round as `mutated` before its output is read. As there, the
    permission rules are the first line and the snapshot is the guarantee.

**Model evidence** comes from `opencode export <session>`: each assistant
message records the `providerID`, `modelID`, `variant` and `agent` it ran with.
The JSON event stream does not carry the model, which is why the script exports
the session after every round. That record is the model OpenCode requested from
the provider; an alias resolved on the provider's side is not visible, which is
one more reason to keep only canonical IDs in the preference list.

The reviewer runs with `--variant high` on both models. OpenCode keeps each
review session in its own session list; the script never deletes them, so
`opencode export <session>` still works if you need to look at a round again.

### Attribution

When a first-pass finding holds up under your verification and you change the
code because of it, the commit that carries that change gets an OpenCode
trailer:

~~~~
Assisted-by: OpenCode:deepseek-flash
~~~~

Write `deepseek-flash` whichever provider ran the rounds. DeepSeek has
announced that it no longer uses versioned model IDs such as
`deepseek-v4.1-flash`, and `deepseek-flash` is the canonical name for the model
both providers serve. The provider-specific ID is how the script reaches the
model through OpenCode Go, not what the trailer records.

Check the session export before writing the trailer all the same. It must show
one of the two models in the preference list; if it shows anything else, the
script has already failed the round and no trailer applies.

The trailer records what shaped the commit, the same rule Stage A and Stage B
follow. A round that ran but changed nothing gets no trailer: a clean
`NO ACTIONABLE FINDINGS`, findings you rejected, or a round that ended
`failed`, `unavailable` or `mutated`. A `blocked` round whose findings you
verified and applied does get one. Stage C gives the order of trailers when
more than one reviewer contributed.


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

Classify every finding from any reviewer before touching an editor:

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
The remaining key is the reviewer, e.g. `claude-fable-5-1`. If the fallback
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
Assisted-by: OpenCode:deepseek-flash
Assisted-by: Codex:gpt-5.6-sol
Assisted-by: Claude Code:claude-fable-5-1
~~~~

Substitute the resolved IDs from Step 2 and Stage B, in review order: OpenCode,
then Codex, then Claude. For OpenCode, always write `OpenCode:deepseek-flash`,
the canonical ID, even when the rounds ran through OpenCode Go under
`opencode-go/deepseek-v4.1-flash`; see the pre-stage's attribution rules. A
first pass that ended `failed` or `unavailable` changed nothing and gets no
trailer; a `blocked` round whose findings you verified and applied does.

 -  **Pre-commit mode** — one commit at the end, carrying whichever trailers
    apply.
 -  **Post-commit mode** — commit after each fix batch, with that reviewer's
    trailer on that commit. Do not rewrite or squash existing commits unless the
    user asks.


Step 3: Report
--------------

Close with a short summary the user can act on:

 -  What each reviewer found, and how many rounds each took. For OpenCode, say
    which of the three outcomes it had: skipped as unavailable (with the
    reason), ran and failed (with the `REASON=` lines), or ran to completion.
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
 -  **Reviewers review; you fix.** Every reviewer is configured read-only.
    Never let any of them edit the repository.
 -  **Never paste diffs into a prompt.** Every reviewer reads the repository
    itself; give it the range and the contract.
 -  **Verify before applying.** A finding is a hypothesis about the code, and
    plausible-sounding ones are wrong often enough to matter.
 -  **Round caps are real.** Two OpenCode rounds, three Codex rounds, two
    Claude rounds. Past that, report instead of looping — the marginal finding
    stops being worth the marginal risk.
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
 -  **The cheap pass is extra, never a substitute.** Whatever OpenCode reports,
    or fails to report, Stage A and Stage B run in full.
 -  **Credit every reviewer that changed the code.** When a verified OpenCode
    finding led to a change, add `Assisted-by: OpenCode:deepseek-flash`, the
    canonical ID, whichever provider served it. A reviewer that ran but
    changed nothing gets no trailer.
 -  **Escalate, do not expand.** Widening scope is the user's call, and it is
    cheap to ask.
