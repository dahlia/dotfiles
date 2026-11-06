---
name: code-review-loop
description: Finish a rough-but-working change set by running it through three independent AI reviewers selected and executed by cross-subagents — a lightweight fast pass, then two frontier models from different vendors (OpenAI, Anthropic, Google, top or second tier) — applying only the fixes that stay inside the original goal, escalating anything that would widen the scope, and committing with the right `Assisted-by` trailers. Use this skill whenever the user asks to "run a code review loop", "review and fix my changes", "polish this before I commit", "마무리 좀 해줘", "코드 리뷰 루프", "리뷰 받고 고쳐줘", "Codex 리뷰", "Claude 리뷰", "OpenCode 리뷰", or otherwise wants a first draft brought up to committable quality by AI review — even when they name only one of the reviewers.
---

Code Review Loop
================

Three steps, one scope contract.

The work is already done and roughly correct; this skill is the finishing pass
that raises it to committable quality. A lightweight, fast model filters the
obvious defects first. Two frontier models from different vendors then review
the code independently, and you commit what survived triage.

`cross-subagents` chooses the routes and runs the CLIs. This skill owns the
scope contract, the round loop, triage, and the guard around protected inputs.
Never substitute your own reading for a failed round: a review that did not run
is unknown, not clean.

The cheap first step exists to protect quota, not to replace anyone. A frontier
round spent on an off-by-one that a small model would have caught is a round
not spent on the problems only a frontier model finds. Steps 2 and 3 run in
full whatever step 1 reports.

The failure mode this skill exists to prevent is scope creep. Every LLM
reviewer will find *something* — that is what it is built to do. Each finding
looks reasonable on its own, so a loop that goes straight from “finding” to
“fix” ratchets outward until the patch swallows refactors, API changes and
pre-existing bugs nobody asked about. The defense is a scope contract written
*before* the first review and a triage step between every finding and every
edit.


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
CROSS="<cross-subagents skill directory>"
GUARD="<this skill's directory>/scripts/review-guard.py"
RUN_ID="review-$(date +%s)-$$"
export REVIEW_OUTPUT_PATHS='[]'           # e.g. '["node_modules/.cache","dist","__pycache__"]'
~~~~

If the cross-subagents cache (`$CROSS_SUBAGENTS_CACHE_DIR`, default
`$XDG_CACHE_HOME/cross-subagents`) resolves inside the repository, add its
relative path to `REVIEW_OUTPUT_PATHS`; otherwise `run.py`'s own artifacts look
like reviewer writes to the guard.

Every file this skill generates lives in `$WORK`, never in the repository. A
prompt or a `review.json` written to the working tree shows up as an untracked
file, which dirties a clean post-commit review and can get swept into the commit
by a `git add -A` in pre-commit mode. Remove `$WORK` only after a successful
loop with no failed, blocked or mutated attempts. Otherwise preserve it and
report its path; it contains private backups and diagnostic logs.

Keep the contract to four short sections:

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


Step 2: Plan the three review steps
-----------------------------------

Plan once, before any reviewer runs. The plan is three independent routes, not
a fallback chain: a clean or early-ended step never cancels the steps after it.

| Step | Role                      | Route constraint                                                                | Round cap |
| ---- | ------------------------- | ------------------------------------------------------------------------------- | --------- |
| 1    | Lightweight fast filter   | Any vendor except OpenAI, Anthropic, Google; `--speed fast`; quality floor 0.60 | 3         |
| 2    | Frontier reviewer         | OpenAI, Anthropic or Google; `--max-tier 2`                                     | 7         |
| 3    | Independent frontier read | OpenAI, Anthropic or Google; different vendor from step 2; `--max-tier 2`       | 5         |

`--max-tier 2` means “top or second tier” within the vendor. The selector ranks
models by task-fit evidence only, so a tight account does not silently demote a
vendor's flagship to a third model. The default `--step-distinct-vendor` keeps
steps 2 and 3 on different vendors, which is what makes the second frontier
read independent.

~~~~ bash
PLAN="$WORK/plan.json"
python3 "$CROSS/scripts/select_route.py" \
  --task code_review --steps 3 \
  --step-spec '{"speed":"fast","quality_floor":0.60,"exclude_vendor":["openai","anthropic","google"]}' \
  --step-spec '{"include_vendor":["openai","anthropic","google"],"max_tier":2}' \
  --step-spec '{"include_vendor":["openai","anthropic","google"],"max_tier":2}' \
  --step-distinct-vendor > "$PLAN"

for i in 1 2 3; do
  jq --argjson i "$i" '.steps[] | select(.step == $i)' "$PLAN" > "$WORK/step${i}.json"
  jq -r --argjson i "$i" '.steps[] | select(.step == $i) |
    "STEP \(.step)\t\(.selected.route_id)\t\(.selected.official_agent)\t\(.selected.official_model_id)\t\(.rationale)"' "$PLAN"
done
jq -r '.unfilled[]? | "UNFILLED step \(.step): \(.reason)"' "$PLAN"
~~~~

`$WORK/step1.json`, `step2.json` and `step3.json` are the authoritative route
for each step. They are what Step 3 reads, and what a re-plan replaces. An
unfilled step has an empty file; skip it and report the reason. Print the table
to the user and keep the rationale with the scope note in `$WORK`.

**If a step is unfilled** (`status: partial` or `no_route_available`), run the
steps that were filled and report the missing one. Do not fill a gap by
repeating another step's model or by quietly dropping to a single frontier
reviewer and calling the loop complete. If no frontier step can be filled, stop
and report the constraint that blocked it.

**If the user names a model or family**, build that step's spec without a
conflicting `include_vendor`, `exclude_vendor`, `max_tier` or `quality_floor`,
and add the pin key (`pin_agent`, `pin_provider` or `pin_model`). The selector
applies pins and vendor filters together; leaving the default vendor rule in
place turns a legitimate pin into `no_route_available`.

**If a route fails mid-loop** (quota, authentication, model unavailable), do
not resume its session on another model and do not let `run.py` fall back
silently. Re-plan that one step with *cumulative* exclusions: every route the
loop has already used, the failed route's vendor, and the other frontier
step's vendor. Keep the arrays across failures; never reset them.

~~~~ bash
STEP=<failed step number>
EXCLUDE_ROUTES=(...)     # every route used so far, plus the failed route
EXCLUDE_VENDORS=(...)    # failed vendor + the other frontier step's vendor
EXCLUDE_ARGS=()
for r in "${EXCLUDE_ROUTES[@]}"; do EXCLUDE_ARGS+=(--exclude-route "$r"); done
for v in "${EXCLUDE_VENDORS[@]}"; do EXCLUDE_ARGS+=(--exclude-vendor "$v"); done

REPLAN="$WORK/step${STEP}.replan.json"
python3 "$CROSS/scripts/select_route.py" --task code_review --steps 1 \
  --step-spec '<role spec>' "${EXCLUDE_ARGS[@]}" > "$REPLAN"

if jq -e '.status == "ok" and (.steps | length) > 0' "$REPLAN" >/dev/null; then
  jq '.steps[0]' "$REPLAN" > "$WORK/step${STEP}.json"   # install the replacement
  jq -r '.rationale' "$WORK/step${STEP}.json"
else
  rm -f "$WORK/step${STEP}.json"                         # leave the step unfilled
  jq -r '.status, (.unfilled[]?.reason // empty)' "$REPLAN"
fi
~~~~

Installing the replacement in `$WORK/step${STEP}.json` is what makes the next
round use it; a plan printed to the terminal is not read again. If the re-plan
fails, remove the file so Step 3 skips the step instead of retrying the failed
route.

For step 1, `<role spec>` is
`{"speed":"fast","quality_floor":0.60,"exclude_vendor":["openai","anthropic","google"]}`.
For steps 2 and 3 it is
`{"include_vendor":["openai","anthropic","google"],"max_tier":2}`.

Global and step-level `--exclude-vendor` are additive in the planner, so the
failed vendor cannot leak back in through the role spec. When no independent
vendor remains for a frontier step, leave it unfilled and report it; never
reuse the other frontier step's vendor. A different model is a different
reviewer: start its rounds from round 1, not by resuming the failed session.


Step 3: Run the steps
---------------------

Each round is one `cross-subagents` invocation wrapped in this skill's
protected-input guard. The guard backs up the repository before the reviewer
starts and compares it afterwards; `run.py` supplies route selection, CLI
session handling, model-substitution detection, and the result contract.

Do **not** pass `run.py --guard` here. `review-guard.py` is the stronger guard:
it archives protected files, tracks nested repositories and Git metadata, and
writes a per-attempt report under `$WORK`. Read-only Git inspection
(`git status`, `git diff`) can refresh the index stat cache; the guard compares
staged entries semantically, so that refresh is not a mutation. Staging,
unstaging or changing a staged entry still is. HEAD, its symbolic ref chain,
and its resolved commit remain protected, including in nested repositories.
Changes to unrelated shared refs from other worktrees and ref packing do not
invalidate the review; loose/packed ref storage is compared by its HEAD meaning.

Pick the prompt template from `references/review-prompts.md`:

 -  step 1 round 1 → “Fast filter — initial review”
 -  step 1 later round → “Fast filter — re-review”
 -  step 2 round 1 → “Frontier — initial review” with role *second step*
 -  step 3 round 1 → “Frontier — initial review” with role *independent final
    read*
 -  step 2/3 later round → “Frontier — re-review”

Substitute the range and the scope contract. Never paste a diff or file
contents into the prompt; the reviewer reads the repository itself.

~~~~ bash
STEP=2
ROUND=1
ATTEMPT=1
STEP_ROUTE=$(jq -r '.selected.route_id' "$WORK/step${STEP}.json")
# No route in the file means the step is unfilled: skip it and report why.
PREFIX="$WORK/step${STEP}-r${ROUND}-a${ATTEMPT}"
PROMPT="$WORK/step${STEP}-r${ROUND}-prompt.txt"

KEEP=""
if [ "$ROUND" = 1 ]; then KEEP="--keep-session"; fi
RESUME=""
if [ "$ROUND" -gt 1 ]; then RESUME="--resume-task step${STEP}-r1"; fi

python3 "$GUARD" --root "$REPO_ROOT" \
  --prefix "$PREFIX" --stdin "$PROMPT" -- \
  python3 "$CROSS/scripts/run.py" \
    --run-id "$RUN_ID" \
    --task-id "step${STEP}-r${ROUND}" \
    --route "$STEP_ROUTE" \
    --task code_review \
    --sandbox read-only \
    --allow-shell \
    --isolate \
    --cwd "$REPO_ROOT" \
    --no-fallback \
    --timeout 900 \
    --prompt-file "$PROMPT" \
    $KEEP $RESUME
~~~~

If `$WORK/step${STEP}.json` is missing or `STEP_ROUTE` is empty or `null`, skip
the step and report it unfilled; never invoke `run.py` with an empty route.

Read the guard result and the task metadata:

~~~~ bash
cat "$PREFIX.exit"
jq -r '.status, .changes' "$PREFIX.guard.json"
jq -r '.status, .error_class, .cleanup_verified, .official_agent,
       .official_model_id, .model_verified, (.observed_model // "-"),
       (.permission_denials // [] | length), (.result_file // "-")' "$PREFIX.stdout"
~~~~

Pass `--prompt-file` explicitly: `run.py` prefers a cached task prompt over
stdin when a task id is reused, so a fresh-prefix retry would otherwise ignore
the revised prompt. The guard archives and feeds the same file. A guard prefix
must be fresh: every retry gets `-a2`, `-a3`, and so on. Never reuse a prefix.

### Classify the round

A round is usable only when **all** of these hold:

 -  `$PREFIX.exit` is the reviewer command's exit code, not 40 or 2;
 -  `$PREFIX.guard.json` has `"status": "unchanged"`;
 -  `$PREFIX.stdout` parses as JSON with `"status": "ok"`;
 -  `cleanup_verified` is `true`;
 -  `permission_denials` is empty (when the adapter reports it);
 -  `result_file` exists and its trimmed text is non-empty.

`guard.json` status `mutated` (exit 40) or a guard failure (exit 2) stops the
whole loop. Preserve `$WORK`, show the changed paths, and never run
`reset --hard`, `clean`, or automatic archive extraction over the user's work.

`cleanup_verified: false` also stops the loop. A surviving child could write
after the guard comparison, so the round did not review a stable tree. Treat a
`null` value as unknown, never as clean.

Classify failures by `error_class` before applying the generic retry rule.
`quota`, `auth` or `model_unavailable` (and `status: quota`) means the step is
unavailable: re-plan it (above) or stop. `timeout`, `transient` or `failed`
gets one fresh-prefix retry with `--prompt-file` pointing at the current
prompt; a second failure ends that step.

A non-empty `permission_denials` is **blocked**, never clean: the reviewer was
refused a tool and may have reviewed whatever it managed to read. Its findings
are still worth triaging, but a blocked round that says
`NO ACTIONABLE FINDINGS` is unknown.

Clean sentinel: the trimmed `result_file` is exactly
`NO ACTIONABLE FINDINGS`. Compare it exactly; silence is not a sentinel.

### Round caps

 -  Step 1: **three rounds** (review plus re-reviews of fixes).
 -  Step 2: **seven rounds**.
 -  Step 3: **five rounds**.

A cap limits how many times you ask the reviewer, not whether you fix what it
found. Apply the last in-scope fixes, verify them yourself, and say in the
report that they carry no further reviewer pass. More rounds past the cap drift
into nitpicking.

### Stop early

A cap is a ceiling, not a target. End a step on the first round that has
nothing new to act on, without spending the remaining rounds:

 -  the round is usable (above) — a blocked, failed, timed-out or `unknown`
    round never justifies a stop;
 -  `result_file` is exactly the clean sentinel, **or** every finding is
    non-actionable under triage — already dispositioned in an earlier round, or
    rejected now as style, speculation, or wrong about the code. A re-review
    repeating a known finding is not a new finding;
 -  the round added no fixes.

A clean round after the last batch of fixes is what closes a step; a clean
round with nothing to verify closes it immediately. Do not stop on silence, an
empty `result_file`, or findings you have not triaged.

Early exit is scoped to the step that earned it. It ends that step's rounds;
it never skips a later step. A step 2 that closes clean on round three still
gets step 3's independent frontier read, and step 3 runs whether step 2 ended
clean, at its cap, or with findings applied. Only an unfilled route or a hard
failure (above) removes a step — a clean earlier pass never does.

Step 1 is advisory. If it ends clean, `failed`, `unavailable` or at its cap, go
on to step 2; never let a cheap pass shorten or replace steps 2 and 3. If step 1
returns in-scope findings, verify and apply them before step 2 so the frontier
reviewers read the improved code.

### Between rounds

For each round: triage every finding (below), apply the in-scope fixes, then —
**in post-commit mode, commit the batch before resuming** (Step 4 rules).
Unless the round ended the step under *Stop early*, resuming without committing
means the reviewer re-reviews a range that still ends at the pre-fix commit; it
would grade code you already changed and report a stale verdict as fresh.

Then write the re-review prompt, naming the previous findings and their
dispositions, and invoke with `--resume-task step${STEP}-r1`. Do not resume on
a different model: a different model is a different reviewer and starts at
round 1.

### When a step cannot run

If `cross-subagents` or its scripts are unavailable, stop and say the loop did
not run. Do not silently replace it with a hand-rolled single-model review; a
gate that did not run is not a passed gate.


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


Step 4: Commit
--------------

**Stage the change set first.** The `commit` skill deliberately commits only
what is already staged and does not touch the index, so anything you leave
unstaged is silently dropped from the commit — and in pre-commit mode the
review covered unstaged and untracked files too, which is exactly where a
review-driven fix tends to land.

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

Leave `$WORK` in place here. In post-commit mode this step runs after every fix
batch, and the next review round still needs the scope contract and the prompt
files inside it.

Then follow the `commit` skill for the message itself.

Add an `Assisted-by` trailer for each reviewer whose findings actually changed
the code — attribution is a record of what shaped the commit, so a reviewer that
found nothing does not get a trailer merely for having run:

~~~~
Assisted-by: OpenCode:kimi-k3
Assisted-by: Codex:gpt-6-astra
Assisted-by: Claude Code:claude-fable-5-1
~~~~

Use the `official_agent` and `official_model_id` from each step's `run.py`
metadata, in step order. The agent name is the official spelling (`Codex`,
`Claude Code`, `OpenCode`, `Antigravity`); the model is the canonical lowercase
ID. Do not copy a stale example. A step that ended failed, unavailable or
blocked-with-nothing-applied gets no trailer.

 -  **Pre-commit mode** — one commit at the end, carrying whichever trailers
    apply.
 -  **Post-commit mode** — commit after each fix batch, with that step's
    trailer. Do not rewrite or squash existing commits unless the user asks.


Step 5: Report
--------------

Close with a short summary the user can act on:

 -  The planned three routes, the actual observed model for each step, and how
    many rounds each took.
 -  Each step's outcome: clean, findings applied, failed, unavailable, or
    blocked. For an unavailable step, give the `error_class` and the route that
    failed. For an unfilled plan step, give the reason. If a failed round left a
    retained session (`session_cleaned_up: false`), include its `session_id`.
 -  What you fixed.
 -  What you rejected, and why (one line each).
 -  **What was deferred**, with the issue it went to and the stopgap you left
    behind, if any.
 -  Which reviewers ran and the quota/tier rationale from the plan output. If
    `model_verified` is false (Codex often does not report its model), say the
    model was requested rather than verified.
 -  Any fixes applied after a round cap, which therefore carry no independent
    reviewer pass.

Report execution modes, allowed output paths, retries and any validation that
could not run. If any attempt failed, was blocked, mutated protected inputs or
lacked verification, preserve `$WORK` and report its path, even when a later
retry succeeded. Keep guard reports and stderr so failures can be diagnosed
without rerunning models. The backup archives can contain private files; do not
upload them or paste their contents into reports.

Remove `$WORK` only when every attempt was usable and no recovery is pending,
after all rounds and authorized commits are finished.

The deferred list matters most. It is the evidence that the loop tightened the
change set instead of expanding it.


Key rules
---------

 -  **The contract comes first.** No reviewer runs before it is written, and
    every finding is measured against it.
 -  **Three steps, no silent substitution.** Step 1 is a filter; steps 2 and 3
    are independent frontier reads. A failed step is re-planned or reported,
    never replaced by another step's model.
 -  **Reviewers inspect and test; you fix.** Allow test outputs in declared
    paths, protect source and Git state, and do not apply reviewer edits
    automatically.
 -  **Never paste diffs into a prompt.** Every reviewer reads the repository
    itself; give it the range and the contract.
 -  **Verify before applying.** A finding is a hypothesis about the code, and
    plausible-sounding ones are wrong often enough to matter.
 -  **Round caps are real, and so is stopping early.** Three, seven, five, but
    end a step on the first usable round with no new actionable findings — and
    only that step. A clean earlier pass never skips a later step. Past the cap,
    report instead of looping — the marginal finding stops being worth the
    marginal risk.
 -  **Keep every generated file in `$WORK`.** Prompts and JSON output written
    into the repository dirty a clean tree and can be swept into the commit.
 -  **Guard every attempt.** A fresh `review-guard.py` prefix per round and
    retry; persistent changes invalidate the review. A snapshot detects
    changes; it does not prevent external effects.
 -  **Stage explicitly before committing.** The `commit` skill commits only what
    is already staged, and pre-commit reviews cover unstaged and untracked
    files.
 -  **A failed review is not a clean review.** Errors, truncation, permission
    blocks and unexpected model substitutions all mean “unknown”, never “no
    issues found”.
 -  **The cheap pass is extra, never a substitute.** Whatever step 1 reports,
    steps 2 and 3 run in full.
 -  **Credit every reviewer that changed the code.** Use the actual
    `Assisted-by: <Agent>:<model>` from that step's metadata. A reviewer that
    ran but changed nothing gets no trailer.
 -  **Escalate, do not expand.** Widening scope is the user's call, and it is
    cheap to ask.
