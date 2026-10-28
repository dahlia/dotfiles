OpenCode CLI reference
======================

Version gate and authentication check
-------------------------------------

OpenCode v2 or later (`>= 2.0.0`) is required when OpenCode is the host
session (`host_usable: true`). Check `opencode --version` before running
discovery or execution commands. If the installed version is `1.x`,
`scripts/probe.py` marks `host_usable: false` and `target_usable: true`,
recording a warning recommending an upgrade to OpenCode v2 while still allowing
v1 to be called as a target CLI if needed. If the version cannot be parsed or
is below `1.0.0`, mark OpenCode unusable. Never upgrade the user's installation
automatically.

Inspect configured providers with:

~~~~ bash
opencode auth list --format json
~~~~

Verify that `opencode auth list --format json` exits with status `0` before
parsing provider records (falling back to text parsing only if `--format json`
is unsupported). OpenCode supports multiple upstream providers simultaneously
(such as `OpenCode Go`, `Anthropic`, `DeepSeek`, `GitHub Copilot`, `Google`,
`OpenAI`, plus the built-in `opencode` provider). Authentication and quota apply
per `(opencode, provider)` pair rather than across the binary as a whole.


Model discovery
---------------

List all models with:

~~~~ bash
opencode models
~~~~

Each line uses the `provider/model` format (for example,
`deepseek/deepseek-flash`, `opencode-go/deepseek-v4.1-flash`,
`anthropic/claude-opus-5-5`). Filter the catalog to providers that appear in
`opencode auth list --format json`, plus `free`-tagged models served under the
built-in `opencode` provider.

During normalization, map provider-specific versioned aliases for the same
underlying model to a shared `canonical_key` (for example, both
`deepseek/deepseek-flash` and `opencode-go/deepseek-v4.1-flash` normalize to
`canonical_key: "deepseek-flash"`), while keeping their `route_id` and
`quota_bucket` (`opencode:deepseek` vs `opencode:opencode-go`) separate.


Quota tracking
--------------

OpenCode does not expose a unified proactive quota command across all connected
providers. Track quota per provider bucket (`opencode:<provider>`) using
observation mode:

 -  Initialize each authenticated provider bucket as `unknown`.
 -  When an invocation fails with HTTP 429 or an exhausted-quota message on
    `opencode:<provider>`, record the observation and cooldown timestamp via
    `scripts/quota.py record-observation --bucket opencode:<provider> --error "<message>"`.
 -  Exhaustion on `opencode:opencode-go` does not mark `opencode:deepseek`
    exhausted; `scripts/select_route.py` can fall back between providers for the
    same canonical model.


Subagent invocation
-------------------

Pass `--standalone` so OpenCode runs against an isolated per-process server
rather than spawning or attaching to a persistent background daemon. Stream the
prompt over standard input with no positional message argument:

~~~~ bash
opencode run --standalone \
  -m <provider>/<model>#<variant> \
  --format json \
  --auto \
  < <prompt-file> > <events.jsonl> 2> <stderr.log>
~~~~

Effort variants (`low`, `medium`, `high`) attach to the model identifier with
`#<variant>` (for example, `-m deepseek/deepseek-flash#high`).

When `--sandbox read-only` is requested, `scripts/run.py` passes `--agent plan`
without `--auto` and sets version-specific `OPENCODE_CONFIG_CONTENT` in the
child environment (in OpenCode v2, a `permissions` array of
`{"action": "<tool>", "resource": "*", "effect": "deny"}` rules for `edit`,
`write`, `patch`, `shell`, and `bash`; in v1, the `permission` map) so
file-modifying and shell tools are blocked while read-only tools run
non-interactively.

Parse `<events.jsonl>` for `"sessionID"` (such as `ses_...`), `"type": "text"`
parts, completion events, and `"type": "error"` events. To capture token usage
and verify the executed model and assistant completion state, export the
session before cleanup:

~~~~ bash
opencode session export --standalone <session-id> > <session-export.json>
~~~~

Normalize the exported JSON root with `.data // .` (some v2 releases wrap the
payload under `data`).

One-shot session cleanup: OpenCode v2 does not have a
`--no-session-persistence` flag on `run`. For one-shot invocations that do not
require resumption, delete the created session immediately after exporting its
result and record `session_cleaned_up` in `meta.json`:

~~~~ bash
opencode session delete --standalone <session-id>
~~~~

Never delete pre-existing user sessions.

Coroutine invocation and resumption:

1.  Keep the session on the initial turn (`--keep-session` in `scripts/run.py`,
    which skips `session delete`).
2.  Resume the session on subsequent turns by passing `-s <session-id>`:

~~~~ bash
opencode run --standalone \
  -m <provider>/<model>#<variant> \
  --format json \
  -s <session-id> \
  --auto \
  < <followup-prompt-file> > <events.jsonl> 2> <stderr.log>
~~~~


Execution postures used by `scripts/run.py`
--------------------------------------------

Read-only execution writes `OPENCODE_CONFIG_CONTENT` with ordered `permissions`
that deny edit/write/patch and, by default, shell/bash. Two postures extend it:

 -  `--allow-shell`: adds `{"action":"shell","effect":"allow"}` and the same
    for `bash`, and injects a dedicated `review-shell` agent with ordered
    permissions (deny all, allow read/glob/grep/shell, deny `.env` reads and
    external directories). A builtin agent can carry its own restrictions, so
    the dedicated agent is what actually makes shell available. Editing tools
    stay denied.
 -  `--isolate`: disables `share`, `update`, and `snapshots`, adds the
    `-opencode.config.instruction` and `-opencode.config.skill` plugins, and
    denies `plugin:*`, `mcp:*`, and `skill:*` integration use.
OpenCode v1 has no per-agent configuration, so `--no-tools` and `--allow-shell`
are rejected on v1 with a permission error instead of reporting enforcement
that was never established.

 -  `--no-tools`: implies `--isolate`, denies `webfetch` too, and injects a
    dedicated `tools-off` primary agent whose only permission rule denies
    everything. `run.py` selects it with `--agent tools-off` instead of the
    builtin `plan` agent, and verifies the agent and permission list before
    execution. This is the only OpenCode posture where "no tool call" is
    enforced by configuration rather than by prompt.
