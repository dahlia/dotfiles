Antigravity CLI reference
=========================

Installation and authentication check
-------------------------------------

Antigravity installs as `agy` (primary CLI binary) and `antigravity`. Check all
matches on `PATH` to detect when a `mise` installation coexists with a system
package such as `/usr/share/antigravity/bin/antigravity`.

`agy` does not provide a standalone `auth status` subcommand. Verify
authentication and backend reachability by running `agy models` and checking
for a zero exit status and non-empty TSV output on standard output.


Model discovery
---------------

Run `agy models`:

~~~~ bash
agy models
~~~~

When standard output is piped, `agy models` writes progress text
(`Fetching available models...`) to standard error and emits
`<model_id>\t<label>` TSV lines on standard output:

~~~~ text
gemini-4-argon-high	Gemini 4 Argon (High)
gemini-3.8-flash-low	Gemini 3.8 Flash (Low)
claude-opus-5-5-high	Claude Opus 5.5 (High)
gpt-oss-120b-medium	GPT-OSS 120B (Medium)
~~~~

Notice two properties of Antigravity's model catalog:

1.  Entries such as `gemini-4-argon-low`, `gemini-4-argon-medium`, and
    `gemini-4-argon-high` bake the reasoning effort suffix into the model ID.
    During normalization, strip the trailing `-<effort>` suffix to derive
    `canonical_key` (`gemini-4-argon`) so capability surveys are shared across
    variants, while preserving the full `model_id` in `route_id`
    (`antigravity/google/gemini-4-argon-high`) and storing the extracted effort
    (`"high"`) in `route["variant"]`.
2.  Antigravity serves Gemini, Claude, and GPT-OSS models under one
    subscription. Infer the underlying model vendor (`google`, `anthropic`,
    `openai`) from `canonical_key` so cross-vendor diversity filters and
    canonical survey lookups match routes from Claude Code, Codex, and OpenCode.


Subagent invocation and stdin delivery
--------------------------------------

In `agy`, `--print` (`-p`) takes the prompt string as its direct flag argument.
Placing `--print` before other flags (such as `agy --print --model ...`) causes
`--print` to consume `--model` as the prompt string and fail with exit code 2.

To pass prompts of arbitrary length over standard input without command-line
length limits or shell quoting hazards on Windows, Linux, or macOS, use
`--input-format stream-json --output-format stream-json` with `--print ""` at
the end of the command line and send a single NDJSON user event on standard
input:

~~~~ bash
printf '%s\n' '{"event":"user","message":{"content":"<json-escaped-prompt>"}}' \
  | agy --model <model-id> \
        --input-format stream-json \
        --output-format stream-json \
        --dangerously-skip-permissions \
        --disable-slash-commands \
        --print-timeout 600s \
        --print "" \
        > <events.jsonl> 2> <stderr.log>
~~~~

For read-only execution (`--sandbox read-only`), pass `--mode plan --sandbox`
instead of `--dangerously-skip-permissions`. For workspace-write execution
(`--sandbox workspace-write`), pass `--mode accept-edits --sandbox`. For
unrestricted execution (`--sandbox bypass`), pass
`--dangerously-skip-permissions`. Avoid `agy remote-control` so no background
daemon is started.

The output stream emits `{"event":"init","conversation_id":"..."}`,
`{"event":"step_update",...}`, and a final
`{"event":"result","result":{"conversation_id":"...","status":"SUCCESS","response":"...","usage":{...}}}`
line.

For coroutine resumption across turns, pass `--conversation <conversation_id>`
before `--print ""`.

Antigravity CLI (`v0.2.2`) persists conversation transcripts under
`~/.gemini/antigravity-cli` and does not expose a session deletion subcommand.
For one-shot invocations, `scripts/run.py` records `session_cleaned_up: false`
with a `cleanup_note` in `meta.json`.


Native delegation inside Antigravity
------------------------------------

When Antigravity itself is the host session, its built-in `invoke_subagent`
tool supports relative model tiers (`inherit`, `flash_lite`, `flash`, `pro`)
rather than arbitrary model IDs. Use `invoke_subagent` when delegating to the
same Gemini model family or when spawning an orchestrator subagent
(`TypeName: "self"` or `"research"`). Use CLI invocation (`scripts/run.py`)
whenever a specific cross-vendor model ID is required.
