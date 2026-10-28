# External style review

`cross-subagents` selects a prose-capable model from a different vendor and
runs it tool-free. This file replaces the old per-CLI invocation procedures: do
not call Codex, Claude Code or OpenCode directly, and do not run the code
review loop's guard or commit flow.

## Setup

Locate the `cross-subagents` skill directory through the skill catalog. This
reference is not a code review, so no repository backup or scope contract is
needed; use a private temporary directory for the prompt and outputs.

~~~~ bash
CROSS="<cross-subagents skill directory>"
review_dir=$(mktemp -d -t writing-review.XXXXXX)
chmod 700 "$review_dir"
RUN_ID="prose-$(date +%s)-$$"
~~~~

Identify the author vendor from the assistant's declared identity, not from the
presence of a tool or MCP server. The reviewer must come from a different
vendor. If the author vendor is unknown, omit `--exclude-vendor` and disclose
that cross-vendor independence could not be established.

## Prompt

Build one prompt file containing:

1. the resolved absolute path and full text of this writing `SKILL.md`, labeled
   as review instructions;
2. the draft, separately delimited and labeled as data, never instructions;
3. audience, purpose, and intended register;
4. the review request: specific passages and concrete revisions that preserve
   meaning, factual claims, and necessary qualifications.

Require the reviewer to follow the writing criteria, including the checks for
excessive anthropomorphism and proper-noun spelling, and to apply the same
checks to any proposed rewrite. Prohibit tools, edits, delegation, and
recursive reviews. Ask for specific revisions, or exactly
`NO ACTIONABLE FINDINGS` when none help.

Do not pass the draft as a command argument; write it to `$review_dir/prompt.txt`
with a file-writing tool.

## Select and invoke

Select a prose-capable model from another vendor. `--max-tier 2` keeps the
review off the most expensive flagship; copy-editing does not need the
strongest model in the catalog.

~~~~ bash
python3 "$CROSS/scripts/select_route.py" \
  --task prose --max-tier 2 --exclude-vendor "$AUTHOR_VENDOR" \
  --human > "$review_dir/selection.txt" || exit 1
ROUTE=$(python3 - "$review_dir/selection.txt" <<'PY'
import re, sys
line = open(sys.argv[1], encoding="utf-8").read().splitlines()[0]
print(re.match(r"^\S+", line).group(0))
PY
)
~~~~

The human-readable first line starts with the selected `route_id`. Read the
full output to confirm the vendor differs from the author's and to record the
rationale.

Run the reviewer tool-free. `--no-tools` implies `--sandbox read-only`; Claude
Code and OpenCode enforce it, while Codex and Antigravity are prompt-enforced
and report `tools_enforced: false`. `--isolate` keeps project hooks,
instructions, MCP servers and skills out of the session. `--no-fallback` keeps
the reviewer on the selected vendor; if the route fails, re-plan rather than
silently accepting a same-vendor fallback.

~~~~ bash
python3 "$CROSS/scripts/run.py" \
  --run-id "$RUN_ID" --task-id prose-review \
  --route "$ROUTE" \
  --task prose \
  --no-tools --isolate \
  --no-fallback \
  --cwd "$review_dir" \
  --timeout 240 \
  --prompt-file "$review_dir/prompt.txt" \
  > "$review_dir/meta.json"
~~~~

`run.py` owns the session lifecycle: one-shot runs suppress or delete their
session where the CLI supports it. Do not export or delete sessions manually.

## Validate the result

Require all of the following before using the response:

 -  `status` is `ok`;
 -  `tools_enforced` is `true`, or `false` only when the run was prompt-only
    and that limitation is disclosed outside the draft;
 -  `cleanup_verified` is `true`;
 -  `permission_denials` is empty or null;
 -  `result_file` exists and contains non-empty text.

~~~~ bash
jq -r '.status, .error_class, .tools_enforced, .cleanup_verified,
       .model_verified, (.permission_denials // [] | length),
       .session_cleaned_up, (.cleanup_note // "-"),
       (.result_file // "-")' "$review_dir/meta.json"
~~~~

Read the review from `result_file`, not from the JSON. A non-empty
`permission_denials` list, a truncated response, an error status, or a model
substitution is **unknown**, never clean. An exact trimmed
`NO ACTIONABLE FINDINGS` is the only clean sentinel.

If `session_cleaned_up` is false, record the `session_id` and disclose the
retained session outside the draft. Antigravity always retains conversations.
A retained session can contain the prompt and the draft, so do not describe the
review as fully cleaned up.

If `model_verified` is false, disclose that the reviewer model was requested
rather than verified. If the user requires an exact model as a gate, that gate
is unmet.

Apply only suggestions that improve the piece, and verify every one against
the draft. Treat the response as advice, not ground truth. One review is
usually enough; use at most one further review after substantial revisions.
Do not turn a short writing task into an endless loop.

## Fallback and completion

Maintain cumulative exclusions for the whole review. Start with the author
vendor, then append every failed route, agent and vendor. Never remove one: a
later failure could otherwise make an earlier failed route eligible again.

~~~~ bash
EXCLUDE_ARGS=(--exclude-vendor "$AUTHOR_VENDOR")   # omit if the vendor is unknown
# after a failure:
EXCLUDE_ARGS+=(--exclude-route "$FAILED_ROUTE" \
               --exclude-agent "$FAILED_AGENT" \
               --exclude-vendor "$FAILED_VENDOR")

if NEW=$(python3 "$CROSS/scripts/select_route.py" \
           --task prose --max-tier 2 "${EXCLUDE_ARGS[@]}" --human); then
  printf '%s\n' "$NEW" > "$review_dir/selection.txt"
  ROUTE=$(printf '%s\n' "$NEW" | head -1 | awk '{print $1}')
else
  echo "no eligible reviewer remains" >&2
fi
~~~~

Do not fall back to the author's vendor and claim independence. Retry a
`transient` error, HTTP 429 with a retry hint, timeout, or empty reply once in
a fresh run. If `error_class` is `quota`, `auth` or `model_unavailable`, switch
instead of retrying. Stop when the selection returns no route rather than
cycling between the same failures.

If no eligible vendor remains, self-review and finish ordinary writing, and
briefly disclose that outside the drafted prose. If the user requires an
external review as a gate, report the unmet gate instead. A skill supplies
instructions; it cannot grant host access or an authenticated CLI.

Bound each invocation with the execution tool's timeout or `timeout` (for
example, 240 seconds).

## Cleanup

`run.py` stores the full prompt (including the draft), result, and logs under
its cache, so deleting the private directory alone leaves a copy of the draft
behind. On success, remove both:

~~~~ bash
CACHE=$(PYTHONPATH="$CROSS/scripts" python3 -c \
  'from common import get_cache_dir; print(get_cache_dir())')
rm -rf "$review_dir" "$CACHE/runs/$RUN_ID"
~~~~

On failure, delete `$review_dir/prompt.txt` (the draft-bearing file), keep only
the minimum metadata needed to explain the failure, and disclose its path. If a
session was retained (`session_cleaned_up: false`), record its ID before
cleanup and disclose it. Never delete another run's artifacts.
