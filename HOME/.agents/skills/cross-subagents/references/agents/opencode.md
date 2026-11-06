OpenCode CLI reference
======================

Version gate and authentication check
-------------------------------------

OpenCode v2 or later (`>= 2.0.0`) is required for both the host session
(`host_usable: true`) and target CLI invocation. Check `opencode --version`
before running discovery or execution commands. If the installed version is
`1.x`, `scripts/probe.py` marks OpenCode unusable (`usable: false`,
`host_usable: false`, `target_usable: false`) and records an upgrade warning.
If the version cannot be parsed or is below `2.0.0`, do not attempt to run it;
select another agent or ask the user to upgrade. Never upgrade the user's
installation automatically.

Duplicate installs are resolved by capability, not by `PATH` order. When a
mise shim resolves to an old global version while a newer version is installed,
`scripts/probe.py` also scans `$MISE_DATA_DIR/installs/opencode/*/`
(`MISE_DATA_DIR` defaults to `~/.local/share/mise`) for
`node_modules/.bin/opencode`, `bin/opencode`, and `opencode`, probes each
candidate with `--version`, and selects the first one whose major version is at
least 2. The choice and every probed candidate are recorded in the warning list
so a surprising binary is explainable.

Inspect configured providers with:

~~~~ bash
opencode auth list --format json
~~~~

Verify that `opencode auth list --format json` exits with status `0` before
parsing provider records (falling back to text parsing only if `--format json`
is unsupported). The text fallback recognizes both the current
`Name  API key  stored` columns and the older `●  Name api` bullet form, so a
credentialed provider is never dropped just because its output shape changed.
OpenCode supports multiple upstream providers simultaneously
(such as `OpenCode Go`, `Anthropic`, `DeepSeek`, `GitHub Copilot`, `Google`,
`OpenAI`, plus the built-in `opencode` provider). Authentication and quota apply
per `(opencode, provider)` pair rather than across the binary as a whole.
`opencode auth list` reports the OpenCode Console credential as `opencode`
(`OpenCode Console`), while `opencode models` exposes two catalogs backed by
that credential: the metered `opencode` (Zen) models and the subscription
`opencode-go` models. Treat both as authenticated when the console credential
exists, and do not drop `opencode-go/*` just because it has no separate auth
entry.

~~~~ bash
opencode models | grep -E '^(opencode|opencode-go)/'
~~~~

Any other provider that `opencode models` lists but `opencode auth list` does not
name (for example `kimi-code-plan-global`, whose credential can be labeled
`Kimi For Coding`) is kept as a `hidden` route with `hidden_reason:
"unverified-provider-credentials"`. The CLI's own catalog is authoritative about
routeability, so an explicit `--route` works without `--force-route`, while
automatic selection still avoids a route whose credentials could not be
cross-checked. `models.py collect` records these provider slugs under
`unverified_providers` in the collection metadata.

When the same canonical model exists in both catalogs, the `opencode-go`
subscription route is selected. Its Zen duplicate remains in the catalog as
`hidden` (reachable with an explicit pin or `--force-route`) so automatic
selection does not attempt a metered route that can fail with
`Insufficient account funds`.


Model discovery
---------------

List all models with:

~~~~ bash
opencode models
~~~~

Each line uses the `provider/model` format (for example,
`deepseek/deepseek-flash`, `opencode-go/deepseek-v4.1-flash`,
`anthropic/claude-opus-5-5`). Accept providers that appear in
`opencode auth list --format json`, plus `opencode-go` when the OpenCode
Console credential is present, plus `free`-tagged models served under the
built-in `opencode` provider. Catalog-only providers are kept as hidden routes
as described above rather than dropped.

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
 -  Initialize `opencode:opencode-go` whenever the OpenCode Console credential
    exists, even though it does not appear as a separate auth entry; failure
    observations are recorded against that bucket.
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
without `--auto` and sets `OPENCODE_CONFIG_CONTENT` in the child environment
(a `permissions` array of
`{"action": "<tool>", "resource": "*", "effect": "deny"}` rules for `edit`,
`write`, `patch`, `shell`, and `bash`) so file-modifying and shell tools are
blocked while read-only tools run non-interactively.

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
    permissions (deny all, allow read/glob/grep/shell, deny `.env` reads). A
    builtin agent can carry its own restrictions, so
    the dedicated agent is what actually makes shell available. Editing tools
    stay denied.
 -  `--isolate`: disables `share`, `update`, and `snapshots`, adds the
    `-opencode.config.instruction` and `-opencode.config.skill` plugins, and
    denies `plugin:*`, `mcp:*`, and `skill:*` integration use.

 -  `--no-tools`: implies `--isolate`, denies `webfetch` too, and injects a
    dedicated `tools-off` primary agent whose only permission rule denies
    everything. `run.py` selects it with `--agent tools-off` instead of the
    builtin `plan` agent, and verifies the agent and permission list before
    execution. This is the only OpenCode posture where "no tool call" is
    enforced by configuration rather than by prompt.

### External directories (`--add-dir` and `--context-file`)

`external_directory` gates every path-taking tool (`read`, `glob`, `grep`, and
many shell commands) that targets a path outside the project worktree. Read-only
runs allow the roots passed with `--add-dir` and the directories holding
`--context-file` entries by appending, for each root,
`{"action":"external_directory","resource":"<root>/*","effect":"allow"}`.
OpenCode evaluates permission rules with **last match wins**, so these allow
rules are appended after any broad `external_directory: deny` (the `review-shell`
agent previously denied every external path, which made `/tmp/...` snapshots
unreadable). Reads inside an allowed directory follow the workspace defaults;
the explicit `edit`/`write`/`patch` denials and `.env` read denials still apply.

Resumed sessions can retain their original tool project when `--cwd` changes.
For OpenCode resumes, `run.py` also grants the effective `--cwd` as an external
directory, even when it matches a saved follow-up cwd. This preserves access
to the requested checkout across repeated resumes without removing read-only
tool restrictions.
