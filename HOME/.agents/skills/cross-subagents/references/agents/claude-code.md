Claude Code CLI reference
=========================

Installation and authentication check
-------------------------------------

Resolve `claude` on `PATH` and record `claude --version`. Run
`claude auth status` to inspect login state:

~~~~ bash
claude auth status
~~~~

The command prints a JSON object containing `loggedIn` (boolean), `authMethod`
(such as `"claude.ai"`), `apiProvider` (such as `"firstParty"`), and
`subscriptionType` (such as `"max"` or `"pro"`). Strip personal fields
(`email`, `orgId`, `orgName`) before writing authentication metadata to
`$SKILL_CACHE_DIR`.


Model discovery
---------------

Query the account's live model catalog at zero token cost by sending an
`initialize` control request over the NDJSON stream protocol:

~~~~ bash
printf '%s\n' '{"type":"control_request","request_id":"init","request":{"subtype":"initialize"}}' \
  | claude --print --input-format stream-json --output-format stream-json \
           --verbose --no-session-persistence
~~~~

`--verbose` is mandatory when `--output-format stream-json` is used; omitting
it causes the CLI to exit immediately with an error.

Read the line with `"type": "control_response"`. Inside `.response.response`,
extract:

 -  `models[]`: each item contains `value` (which may be a moving alias such as
    `default`, `opus`, `fable`, `sonnet`, `haiku` or a full model ID),
    `resolvedModel` (the canonical model ID, such as `claude-opus-5-5` or
    `claude-fable-5-1`), `displayName`, `description`, `supportsEffort`,
    `supportedEffortLevels`, `supportsAdaptiveThinking`, and `supportsFastMode`.
 -  `account`: `subscriptionType` (for example, `"Claude Max"`) and
    `apiProvider`.
 -  `claude_code_version`.

Normalize and deduplicate routes by `resolvedModel` while keeping any moving
alias (`fable`, `opus`, `sonnet`, `haiku`) in route metadata. Discard the
`commands`, `skills`, and `agents` arrays from the response before caching.


Quota inspection
----------------

Query subscription quota buckets at zero token cost using the `/usage` slash
command in print mode:

~~~~ bash
claude -p "/usage" --output-format json --no-session-persistence
~~~~

The `.result` string contains human-readable usage lines per quota window:

~~~~ text
Current session: 4% used · resets Oct 28, 11:50pm (Asia/Seoul)
Current week (all models): 75% used · resets Oct 31, 10pm (Asia/Seoul)
Current week (Fable): 100% used · resets Oct 31, 10pm (Asia/Seoul)
~~~~

Model families such as Fable have a dedicated weekly quota bucket
(`claude-code:fable`) separate from `claude-code:all-models`. When
`claude-code:fable` is at 100% while `claude-code:all-models` has remaining
capacity, Fable routes are filtered out while Opus, Sonnet, and Haiku remain
eligible.

If the output format changes and regex parsing extracts no lines, record the
quota status as `unknown` rather than `0%`.


Subagent invocation
-------------------

One-shot invocation (leaves no entry in the user's interactive session picker):

~~~~ bash
claude -p --model <model-or-alias> \
  --effort <low|medium|high|xhigh|max> \
  --output-format json \
  --no-session-persistence \
  --dangerously-skip-permissions \
  < <prompt-file> > <claude-output.json> 2> <stderr.log>
~~~~

Extract `.result` for the response text, `.usage` (`input_tokens`,
`output_tokens`) for token accounting, `.is_error` for error status, and
`.modelUsage` to verify the exact model ID that served the request (ignoring
internal `claude-haiku-*` bookkeeping entries). `modelUsage` can also list
auxiliary models used by subagents; treat the requested model as verified when
it appears in the map, and only report substitution when it is absent and a
different non-Haiku model is present.

The CLI emits a single JSON document, but warnings can precede it on stdout.
Parse the last valid JSON object rather than slicing from the first `{` to the
last `}`; `run.py` does this and reports `Format` failures with an explicit
"not valid JSON" message when nothing parses.

Coroutine invocation and resumption:

1.  On the initial turn, omit `--no-session-persistence`. Read `session_id`
    from `<claude-output.json>` (or pass `--session-id <uuid>`).
2.  On subsequent turns, pass `--resume <session-id>` without `--session-id`
    (passing both `--session-id` and `--resume` without `--fork-session` causes
    Claude Code to exit with an error):

~~~~ bash
claude -p --resume <session-id> \
  --model <model-or-alias> \
  --effort <level> \
  --output-format json \
  --dangerously-skip-permissions \
  < <followup-prompt-file> > <claude-output.json> 2> <stderr.log>
~~~~


Self-healing and host recursion stats
-------------------------------------

When a flag fails with an unknown option error, inspect `claude --help` to
identify updated flag names. Treat that help text as a hint, not as the
capability list: Claude Code omits some supported options from `--help` (2.1.312
lists no `--tools`) and truncates the output when stdout is a pipe, so a
read-only posture can look unsupported while the flag works. `scripts/probe.py`
confirms every flag the help does not show by running the CLI with that flag
plus an unknown sentinel option, which makes the parser fail before any auth or
model call. When Claude Code runs as the host agent and spawns native subagents,
check `subagent_stats.refused.{depth_limit,concurrency_limit,budget}` in JSON
outputs so host-level refusals are not double-counted in
`$SKILL_CACHE_DIR/registry/`.


Execution postures used by `scripts/run.py`
--------------------------------------------

Read-only execution passes `--permission-mode dontAsk`, `--permission-prompts
none`, `--tools`, and `--disallowedTools`. Three additional postures layer on
top. `run.py` re-checks every flag the cached recipe marked unsupported against
the CLI's argument parser before refusing a posture, so a recipe built from
truncated `--help` output cannot block a supported read-only run. The postures
are:

 -  `--no-tools`: `--tools ""` plus `--safe-mode` and `--strict-mcp-config`.
    No tool may run; this is the posture for prose review. `--tools ""` is
    accepted by Claude Code with a JSON result even though the option is
    variadic, because the empty string is the value and the next token starts
    with `--`.
 -  `--allow-shell`: adds `Bash` to `--tools` and `Bash(*)` to `--allowedTools`
    while edit/write/notebook tools stay denied. Use it when a review needs
    focused tests or git inspection.
 -  `--isolate`: adds `--safe-mode` (no hooks, project instructions, or
    auto-memory) and `--strict-mcp-config` (no MCP servers). The reviewer reads
    repository guidance files itself instead of inheriting them.

`run.py` records the requested posture as `tool_policy` and whether the CLI can
enforce it as `tools_enforced`.
