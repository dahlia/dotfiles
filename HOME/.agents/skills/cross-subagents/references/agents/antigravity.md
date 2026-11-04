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

Older `agy` builds (seen with 1.4.10) do not accept `--mode` or `--sandbox`.
In that case `scripts/run.py` refuses read-only and workspace-write execution
with `error_class: no_option` instead of silently running unsandboxed. Use
`--sandbox bypass` when the user accepts unrestricted execution, or select a
different agent for those postures.

The output stream emits `{"event":"init","conversation_id":"..."}`,
`{"event":"step_update",...}`, and a final
`{"event":"result","result":{"conversation_id":"...","status":"SUCCESS","response":"...","usage":{...}}}`
line.


Read access outside the working directory
-----------------------------------------

Headless `agy` cannot open an approval prompt. A `read_file` call for a path
outside the session workspace is auto-denied even under
`--mode accept-edits --sandbox` (workspace-write) and `--mode plan --sandbox`
(read-only). The turn still ends with
`{"event":"result","result":{"status":"SUCCESS","response":""}}`, and the only
evidence is a stderr note:

~~~~ text
jetski: no output produced — a tool required the "read_file" permission that
headless mode cannot prompt for, so it was auto-denied. Add an allow-rule under
permissions.allow in settings.json (e.g. read_file(<target>)). Alternatively,
re-run with --dangerously-skip-permissions to auto-approve all tools.
~~~~

Pass `--add-dir <absolute-path>` (repeatable, before `--print ""`) for every
external root the prompt references. Adding the directory to the workspace is
the supported way to grant those reads without `--dangerously-skip-permissions`:

~~~~ bash
agy --model gemini-4-argon-high \
    --add-dir /home/alice/Projects/deno \
    --mode accept-edits --sandbox ...
~~~~

`scripts/run.py` forwards its own `--add-dir` (plus the directories holding
`--context-file` entries) this way, and classifies the auto-denial as
`permission`: it records the denied tool and target in `denials_observed` and
does not fall back to another model, because a headless approval gap is
deterministic. A `permissions.allow` rule in settings.json
(`read_file(<absolute-path>)`, `command(<binary> <subcommand>)`,
`write_file(<absolute-path>)`) is an alternative for roots reused across runs;
never use the blanket `read_file(*)`/`read_file(/)` form.

Shell commands in read-only review
----------------------------------

A read-only review that passes `--allow-shell` still needs a `command` approval,
which headless mode auto-denies the same way (`a tool required the "command"
permission ... auto-denied`; the turn ends empty with `status: SUCCESS`). Grant
it with a session-only settings override that carries an allow-rule:

~~~~ bash
agy --mode plan --sandbox --disable-slash-commands \
    --settings '{"permissions":{"allow":["command(*)"]}}' ...
~~~~

This does not weaken the posture: `--sandbox` mounts a read-only filesystem, so
a `touch` returns `Read-only file system` even with `command(*)` allowed
(verified against `agy` 1.4.12). `scripts/run.py` builds this override from the
user's `~/.gemini/antigravity-cli/settings.json` when `--allow-shell` is set,
merging any existing `permissions` so a session override cannot drop a user's
deny rule.

`--mode plan` emits `warning: --mode plan has no effect while slash command
expansion is disabled.` when combined with `--disable-slash-commands`, which
`scripts/run.py` passes. Read-only execution therefore relies on `--sandbox`
alone for mechanical enforcement (it enforces a read-only filesystem), and on
allow-rules for the inspection tools the run needs. Inspect `denials_observed`
when a read-only run looks incomplete.

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
