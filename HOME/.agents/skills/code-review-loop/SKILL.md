---
name: code-review-loop
description: Finish a rough-but-working change set by running it through independent AI reviewers — a cheap first pass with protected inputs through OpenCode on DeepSeek Flash when OpenCode is available, then an iterative `codex review` loop on OpenAI's frontier model, then a final `claude -p` pass on Claude's frontier model — applying only the fixes that stay inside the original goal, escalating anything that would widen the scope, and committing with the right `Assisted-by` trailers. Use this skill whenever the user asks to "run a code review loop", "review and fix my changes", "polish this before I commit", "마무리 좀 해줘", "코드 리뷰 루프", "리뷰 받고 고쳐줘", "Codex 리뷰", "Claude 리뷰", "OpenCode 리뷰", or otherwise wants a first draft brought up to committable quality by AI review — even when they name only one of the reviewers.
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
by a `git add -A` in pre-commit mode. Remove `$WORK` only after a successful loop with no failed, blocked or mutated
attempts. Otherwise preserve it and report its path; it contains private backups
and diagnostic logs.

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

Codex and Claude run on a frontier model family chosen by how much
subscription quota the connected accounts have left, and always on the newest
model of that family:

| Quota  | Codex                   | Claude Code                   |
| ------ | ----------------------- | ----------------------------- |
| Ample  | newest **Astra** model  | newest **Fable** (`fable`)    |
| Tight  | newest **Sol** model    | newest **Opus** (`opus`)      |

The top tier is the better reviewer, and a reviewer that misses the defect
costs more than the round it saved. That holds even when the change was
implemented on a cheaper model. But a review loop can burn several rounds per
reviewer, and on a nearly spent weekly quota that leaves the user unable to
work for the rest of the week; the second tier still finds most of what
matters. The two quotas are independent, so decide each reviewer separately:
Codex can be on Sol while Claude stays on Fable.

Resolve rather than hardcode, so the skill keeps working after the next
release. If the user names a model or family for a reviewer, use that instead
and skip the quota check for it.

**OpenCode** — the one exception: the first pass is deliberately *not* a
frontier model, and its model list is fixed in a deterministic preference
order. See the pre-stage below.

Run the picker once, before any reviewer:

~~~~ bash
PICK_MODELS="<this skill's directory>/scripts/pick-models.sh"
bash "$PICK_MODELS" > "$WORK/models.txt"
eval "$(grep -E '^(CODEX|CLAUDE)_[A-Z]+=' "$WORK/models.txt")"
grep '^NOTE=' "$WORK/models.txt"      # the reasoning, for your report
~~~~

It takes about 15 seconds and sets `CODEX_MODEL`, `CODEX_QUOTA`,
`CLAUDE_MODEL`, `CLAUDE_FALLBACK` and `CLAUDE_QUOTA`.

**How quota is read.** Codex: the `account/rateLimits/read` call on
`codex app-server`, the same numbers `/status` shows. Claude Code: the
`rate_limit_event` that `claude -p --output-format stream-json` emits, from a
one-line Haiku probe that costs next to nothing. Both report percent used per
window (Codex's weekly window; Claude's five-hour and seven-day windows).

**What counts as tight.** A window is tight when it is at least 80% used, or
at least 50% used and being spent faster than time is passing (used % above
the share of the window already elapsed), because at that pace it runs out
before it resets. A quota is tight when any of its windows is, or when a limit
is already reached. At 75% used with half the week left, Codex is tight; at
47% with an hour to reset, Claude is ample. If the quota cannot be read, the
picker reports `unknown` and uses the top tier — the usage-limit fallbacks in
Stage A and Stage B catch a real shortage, while a guessed “tight” would
silently downgrade every review.

**Codex** — the picker takes the newest model of the chosen family from Codex's
own model catalog (`codex debug models`, listed models only, highest version
first), so it is a model this account can actually run. It yields a slug like
`gpt-6-astra` or `gpt-6-sol`. If `CODEX_MODEL` comes back empty, fall back to
the flagship the OpenAI docs publish when the family is Astra:

~~~~ bash
curl -sL https://developers.openai.com/api/docs/guides/latest-model.md \
  | sed -n 's/^ *model: *//p' | head -1
~~~~

and otherwise to the `model` value in `~/.codex/config.toml`, and say in your
report which source you used. `codex review` echoes `model:` in its header —
confirm it matches before trusting the output.

**Claude** — the moving aliases `fable` and `opus` always resolve to the
newest model of their family, so pass the alias and read the exact ID back out
of the response, which is both simpler and more accurate than guessing the
version suffix. See Stage B.

Say in your report which family each reviewer ran on and why, quoting the
`NOTE=` lines.


Shared runner: backups and test execution
------------------------------------------

Use `scripts/review-guard.py` around every reviewer invocation, including retries
and resumed rounds. The default runs in the original working directory. It
backs up protected files before the reviewer starts, compares persistent state
after it exits, and saves stdout, stderr and the command exit code. It does not
restore automatically or prevent writes outside the repository.

~~~~ bash
GUARD="<this skill's directory>/scripts/review-guard.py"
# Declare only disposable output directories needed by this repository's checks.
# No exclusions by default. Ignored files are still protected.
export REVIEW_OUTPUT_PATHS='[]'
~~~~

Set `REVIEW_OUTPUT_PATHS` to a JSON array of relative directories, for example
`["node_modules/.cache", "dist"]`, after inspecting the repository's checks.
Tracked files remain protected even inside these directories. Do not exclude
source, fixtures, manifests, lockfiles, environment files or an entire dependency
tree merely because Git ignores it. Existing ignored files outside these output
paths are backed up too; on large trees this costs disk space and time.

Prompts must tell every reviewer the allowed output paths. Permit focused tests,
builds and type checks, including temporary files and normal runtime caches.
Keep fixes, source generation that changes protected inputs, dependency installs,
Git mutations and external service changes with the author. Repository-local
outputs must stay in the declared directories. Use existing test dependencies;
report missing prerequisites instead of installing them or starting services.

Each attempt needs a fresh prefix outside the repository. The runner creates
`PREFIX.backup.tar`, before/after JSON manifests, `PREFIX.guard.json`, `.stdout`,
`.stderr` and `.exit`. The archive preserves file bytes, modes and symlink targets,
including untracked/ignored files, plus the index and relevant Git metadata.
HEAD, refs and merge/rebase state are checked. Git object storage, reflogs, external
symlink targets and machine-wide state are not backed up. Submodules and other
nested repositories get the same treatment: their working trees are walked like
any directory, and their Git state (HEAD, index, refs, config) is checked in a
`git[<path>]` area of the guard report, even when it lives under the
superproject's `.git/modules/`, so a reviewer committing or switching branches
inside a submodule is caught. Gitlinks with no repository behind them, such as
uninitialized submodules, need nothing extra. If a nested `.git` cannot be
resolved to a repository, the runner refuses rather than claim coverage.
Do not edit the repository concurrently with a review. A change during backup
aborts the attempt; a change during review cannot reliably be attributed to it.

A guard report with `status=mutated` returns exit 40. Without a completed guard
report, exit 2 means verification failed. Otherwise the runner returns the
reviewer command exit code, which can also be 40 or 2; inspect the report before
classifying the exit. Neither is a usable review. Preserve the artifacts,
show the changed paths, and stop before triage or another reviewer. Never run
`reset --hard`, `clean`, or automatic archive extraction over the user's work.
Recover only identified changes after checking for concurrent edits. Backups are
recovery material and persistent-change detection, not a sandbox or proof that
no transient write occurred. They can contain secrets; keep `$WORK` private.

An independent temporary copy is an optional execution mode when dependencies
can be reproduced cheaply. It needs independent Git metadata and the exact
staged, unstaged and untracked inputs; a plain worktree shares Git metadata and
does not reproduce dirty state. Do not substitute this mode without verifying
those inputs. Keep dependencies independent too: hard links or external symlinks
can let test writes reach the original. The common runner currently supports the
original-directory mode, not automatic cloning or restoration.


Pre-stage: the OpenCode first pass
----------------------------------

A small, cheap model reads the change set before Codex does. Its job is to
catch the defects that are obvious once someone looks (wrong conditions,
off-by-one errors, unhandled errors, typos in identifiers) so that Codex and
Claude spend their rounds on the rest. It is advice from a weaker reviewer:
every finding goes through triage like any other, and nothing about this stage
shortens, skips or replaces Stage A or Stage B.

All of the mechanics live in `scripts/opencode-review.sh` next to this file.
Use the script rather than retyping its commands: the shared runner and
result checks only hold if every round runs them
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
`LOG_PREFIX.review.txt`. The script prints `LOG_PREFIX` for each attempt and
uses a new directory per invocation, so provider fallbacks cannot overwrite
earlier evidence. Its exit code is the status:

| Exit | Status        | Meaning                                                   | What you do                                    |
| ---- | ------------- | --------------------------------------------------------- | ---------------------------------------------- |
| 0    | `clean`       | The final answer is exactly `NO ACTIONABLE FINDINGS`      | End the stage                                  |
| 10   | `findings`    | A complete review with findings                           | Triage, fix, re-review (below)                 |
| 20   | `failed`      | The reviewer ran, but the round is not a usable review    | End the stage; report it as failed             |
| 21   | `blocked`     | A complete answer, but a tool call was denied             | As `findings`, but never count it as clean     |
| 31   | `transient`   | Known transient provider/transport error or empty final reply | Retried once automatically; then try next provider in round 1 |
| 30   | `unavailable` | Known authentication, model or exhausted-quota error | Round 1: next model. Later rounds: as `failed` |
| 41   | `guard_failed` | Protected inputs could not be verified | Stop the whole loop and preserve evidence |
| 40   | `mutated`     | The repository changed while the reviewer ran             | Stop and show the user, as for any reviewer    |

On round 1, `unavailable` or exhausted `transient` moves on to the next model in the list, starting a
fresh session. If every model comes back `unavailable`, the stage is skipped as
unavailable, and the report lists each model's `REASON=` lines. Once a model has
served round 1, it is the stage's model: record `MODEL` and `SESSION` from the
status line and use both for the next round. Never resume a session on another model. Later-round provider failures end the
stage; do not switch silently. Infrastructure retries do not consume the two
review rounds. The script permits at most two attempts per provider per round;
with the two configured providers, round 1 has at most four attempts.

The classifier rejects nonzero command exits, stream/assistant errors, missing
exports or assistant messages, unexpected model/variant/agent, truncated final
steps and empty final text. Known authentication/model/quota errors become
`unavailable`; HTTP 429/5xx, known transport failures, timeouts and empty final
replies become `transient`. Unexpected model substitutions or truncated replies
remain failed even if another diagnostic suggests retrying. Other local failures
are `failed`, not provider unavailability. `OC_TIMEOUT` defaults to 900 seconds
per attempt; `timeout --kill-after=10` bounds a process that ignores termination.

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

When the stage ends as `failed`, exhausted `transient`, or `unavailable` on every
model, go on to Stage A. A `mutated` or `guard_failed` result stops the whole loop until
the inputs are verified again. Never
substitute your own reading and report it as OpenCode's verdict.

### Permissions and protected inputs

The dedicated `review-loop-flash` agent denies editing tools, delegation, web
and MCP tools. Read/glob/grep/list and Bash are allowed so the reviewer can run
focused checks and ordinary inspection commands, including pipelines and
`git -C`. Bash permission is intentionally broad; a prompt is not an enforcement
boundary. The shared runner backs up and checks protected inputs around each
attempt. `--auto` cannot override an explicit deny and is not needed here.

Project config, external plugins, Claude prompts, external skills, sharing and
auto-update remain disabled. Preflight verifies the dedicated agent and disabled
edit/write/task/webfetch tools. Keep preflight/configuration stderr under `$WORK`
when diagnosing a failure; do not print resolved configuration credentials.

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

### Choosing execution permissions

Use `workspace-write` when it supports the repository's checks. A successful
`git rev-parse` probe establishes only that basic inspection works, not that tests
can write their outputs, use caches or reach a required local test dependency.
Choose the mode from the actual checks and host behavior. Do not use read-only
execution for a stage that permits tests with filesystem output.

On hosts where Codex's sandbox cannot start, or required local checks remain
blocked, use `danger-full-access` intentionally with the shared runner. Set
`approval_policy=never` for noninteractive execution. This removes local sandbox
restrictions, including outside the repository; the backup does not replace
that boundary. Record the mode and reason in the report. Do not change global
Codex configuration. Check the installed CLI's options before adapting these
commands to permission profiles or a newer CLI.

### Running a round

~~~~ bash
CODEX_SANDBOX=workspace-write   # or danger-full-access for the reason above
codex --version > "$WORK/codex-version.txt" 2>&1
python3 "$GUARD" --root "$REPO_ROOT" \
  --prefix "$WORK/codex-r$ROUND-a$ATTEMPT" \
  --stdin "$WORK/codex-prompt.txt" -- \
  codex review \
    -c model="$CODEX_MODEL" \
    -c model_reasoning_effort="high" \
    -c sandbox_mode="$CODEX_SANDBOX" \
    -c approval_policy=never -
~~~~

Set `ROUND` and `ATTEMPT` before invoking the command. Read `.stdout`, `.stderr`,
`.exit` and `.guard.json` from that prefix. Do not interpret the runner's exit 0
alone as a clean verdict. Every retry needs a new attempt prefix and backup.

Build `$WORK/codex-prompt.txt` from the template in
`references/review-prompts.md` (section “Codex — initial review”), substituting
the range and the scope contract. Findings appear at the end of the output; the
final block is repeated once, so read it, do not count it twice.

If a round on an Astra model fails with a usage limit error, the quota ran
out mid-loop: switch `CODEX_MODEL` to the newest Sol model (`bash
"$PICK_MODELS"` would pick it now; or take it from `codex debug models`), re-run
that round once, and note the switch in your report. If Sol also fails, or the
loop was already on Sol, end Stage A as failed and report it.

Before trusting a round, verify the requested model and execution permissions
in its header and inspect failed/denied tool calls. A blocked necessary read or
check makes the review incomplete, whatever its closing text says. Correct the
execution setup and retry the round once. Use `danger-full-access` when a sandbox
failure is the cause and report why. A protected-input mutation or guard failure
stops the loop; never continue on unverified inputs.

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

claude --version > "$WORK/claude-version.txt" 2>&1
python3 "$GUARD" --root "$REPO_ROOT" \
  --prefix "$WORK/claude-r$ROUND-a$ATTEMPT" \
  --stdin "$WORK/claude-prompt.txt" -- \
  claude -p \
    --model "$CLAUDE_MODEL" \
    ${CLAUDE_FALLBACK:+--fallback-model "$CLAUDE_FALLBACK"} \
    --effort high \
    --session-id "$SESSION_ID" \
    --safe-mode \
    --strict-mcp-config \
    --permission-mode dontAsk \
    --output-format json \
    --tools Read Grep Glob Bash \
    --disallowedTools Edit Write NotebookEdit "mcp__*" \
    --allowedTools "Bash(*)"
~~~~

Set `ROUND` and `ATTEMPT` first. The final JSON is in the prefix's `.stdout`;
set `CLAUDE_RESULT` to that path for the commands below. Bash is allowed for
inspection and tests; direct editing tools and MCP remain disabled. Safe mode
keeps hooks, skills and automatic repository instructions out of the session;
the prompt asks the reviewer to read contributor instructions itself.

The shared runner protects repository inputs. A broad Bash rule can still write
files or affect external services, so do not describe `dontAsk` or disabled
editing tools as a filesystem guarantee. Retain the prompt's limits on source,
Git state, dependency installation and service changes.

Pass the prompt through the runner's stdin file. Claude's tool-list options are
variadic and can swallow a trailing prompt argument.


Read the result:

~~~~ bash
jq -r '.result' "$CLAUDE_RESULT"                        # the review
jq -r '.is_error, .subtype, .api_error_status' "$CLAUDE_RESULT"
jq -r '.permission_denials' "$CLAUDE_RESULT"            # blocked tools
jq -r '.modelUsage | keys[]' "$CLAUDE_RESULT" | grep -v '^claude-haiku'
~~~~

Check `permission_denials` every round, not just when something looks wrong. A
reviewer that was denied `git diff` will still produce a confident-sounding
review — of whatever it managed to read instead. That is the quietest way this
stage fails, and the output gives no sign of it.

The last one is what goes in the trailer. `modelUsage` always contains a
`claude-haiku-*` entry for Claude Code's own internal bookkeeping — ignore it.
The remaining key is the reviewer, e.g. `claude-fable-5-1` (or
`claude-opus-5-5` when the picker chose `opus`). If the fallback engaged you
will see an Opus ID where you expected Fable; attribute the model that
actually did the work and mention the fallback in your report.

**On quota exhaustion**, `--fallback-model opus` handles routing automatically
for overload and unavailability when the run is on Fable. If the invocation
itself fails with a usage limit error, retry once with `--model opus` (from
Fable) — on a later round, as a fresh session from the initial template, since
a different model resuming the session is a different reviewer. If the run
was already on Opus, or the retry also fails, stop and report
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
redirection. `$WORK` is cleaned up after Step 3 only when all attempts were usable.

Then follow the `commit` skill for the message itself.

Add an `Assisted-by` trailer for each reviewer whose findings actually changed
the code — attribution is a record of what shaped the commit, so a reviewer that
found nothing does not get a trailer merely for having run:

~~~~
Assisted-by: OpenCode:deepseek-flash
Assisted-by: Codex:gpt-6-astra
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
    which of the outcomes it had: skipped as unavailable (with the
    reason), ran and failed (with the `REASON=` lines), or ran to completion.
 -  What you fixed.
 -  What you rejected, and why (one line each).
 -  **What was deferred**, with the issue it went to and the stopgap you left
    behind, if any.
 -  Which models ran, including any fallback, and the quota reading that
    chose each family (the `NOTE=` lines from `pick-models.sh`).
 -  Any fixes applied after a round cap, which therefore carry no independent
    reviewer pass.

Report execution modes, allowed output paths, retries and any validation that
could not run. If any attempt failed, was blocked, mutated protected inputs or
lacked verification, preserve `$WORK` and report its path, even when a later
retry succeeded. Keep version files, stderr, session exports and guard reports
so failures can be diagnosed without rerunning models. The backup archives can
contain private files; do not upload them or paste their contents into reports.

Remove `$WORK` only when every attempt was usable and no recovery is pending,
after all rounds and authorized commits are finished.

The deferred list matters most. It is the evidence that the loop tightened the
change set instead of expanding it.


Key rules
---------

 -  **The contract comes first.** No reviewer runs before it is written, and
    every finding is measured against it.
 -  **Reviewers inspect and test; you fix.** Allow test outputs in declared paths,
    protect source and Git state, and do not apply reviewer edits automatically.
 -  **Never paste diffs into a prompt.** Every reviewer reads the repository
    itself; give it the range and the contract.
 -  **Verify before applying.** A finding is a hypothesis about the code, and
    plausible-sounding ones are wrong often enough to matter.
 -  **Round caps are real.** Two OpenCode rounds, three Codex rounds, two
    Claude rounds. Past that, report instead of looping — the marginal finding
    stops being worth the marginal risk.
 -  **Keep every generated file in `$WORK`.** Prompts and JSON output written
    into the repository dirty a clean tree and can be swept into the commit.
 -  **Back up and compare around every attempt.** Persistent changes invalidate
    the review. A snapshot detects changes; it does not prevent external effects.
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
