Codex CLI reference
===================

Installation and authentication check
-------------------------------------

Locate all `codex` binaries on `PATH` (`which -a codex` on Linux and macOS,
`where codex` on Windows). When a `mise` shim (`*/shims/codex`) precedes a
global npm or binary install, resolve the active target with `mise which codex`
and record the version from `codex --version`.

Check authentication with `codex login status`. A usable account prints
`Logged in using ChatGPT` (or an API key status). For deeper diagnostics,
`codex doctor` reports `install method`, `stored auth mode`, `stored API key`,
and whether the background `app-server` is running in persistent mode.


Model discovery and quota inspection
------------------------------------

Do not call `https://api.openai.com/v1/models` with the token from
`~/.codex/auth.json` (or `$CODEX_HOME/auth.json`). Accounts authenticated
through ChatGPT store a token scoped to `chatgpt.com/backend-api`; sending it
to `api.openai.com` fails with HTTP 403.

Instead, query `codex app-server` over standard input and output using JSON-RPC
2.0:

~~~~ json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"cross-subagents","version":"0.1.0"}}}
{"jsonrpc":"2.0","method":"initialized"}
{"jsonrpc":"2.0","id":2,"method":"model/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"account/rateLimits/read","params":{}}
~~~~

Immediately after `initialize`, the server emits an `account/updated`
notification containing `authMode` and `planType` (for example, `"promax"`).

The `model/list` response returns `result.data[]`, where each entry provides
`id`, `displayName`, `description`, `hidden`, and `supportedReasoningEfforts`
(`low`, `medium`, `high`, `xhigh`, `max`, `ultra`).

The `account/rateLimits/read` response returns:

 -  `ordinaryUsageAllowed`: boolean gate indicating whether standard requests
    are currently permitted.
 -  `rateLimits` and `rateLimitsByLimitId`: quota windows keyed by `limitId`
    (such as `"codex"`), each containing `primary.usedPercent`,
    `primary.windowDurationMins` (`10080` for the 7-day weekly window),
    `primary.resetsAt` (Unix epoch seconds), `credits` (`hasCredits`,
    `balance`), `planType`, and `rateLimitReachedType`.
 -  `rateLimitResetCredits`: `availableCount` and `credits[]` (`id`,
    `grantedAt`, `expiresAt`, `status`, `resetType`).

To consume an eligible free reset credit programmatically when a run is blocked
on quota and the 30-minute auto-redeem policy is met, send:

~~~~ json
{"jsonrpc":"2.0","id":4,"method":"account/rateLimitResetCredit/consume","params":{"creditId":"<id>","idempotencyKey":"<unique-key>"}}
~~~~


Subagent invocation
-------------------

Place `--no-daemon` before the `exec` subcommand. In `codex-cli`, `--no-daemon`
is a root flag; passing `codex exec --no-daemon` fails with an unexpected
argument error.

One-shot invocation (no session files left on disk):

~~~~ bash
codex --no-daemon exec \
  --ephemeral --skip-git-repo-check \
  --json -m <model> \
  -c model_reasoning_effort="high" \
  -c sandbox_mode="workspace-write" \
  -c approval_policy=never \
  -o <result-file> \
  - < <prompt-file> > <events.jsonl> 2> <stderr.log>
~~~~

Coroutine invocation and resumption:

1.  Omit `--ephemeral` on the initial turn so Codex persists the thread.
2.  Read `thread_id` from the `{"type":"thread.started","thread_id":"..."}`
    event in `<events.jsonl>`.
3.  Resume the thread on subsequent turns with `codex --no-daemon exec resume`:

~~~~ bash
codex --no-daemon exec resume \
  --skip-git-repo-check --json -m <model> \
  -c model_reasoning_effort="high" \
  -c sandbox_mode="workspace-write" \
  -c approval_policy=never \
  -o <result-file> \
  <thread-id> - < <followup-prompt-file> > <events.jsonl> 2> <stderr.log>
~~~~

When the host environment cannot start Codex's local sandbox or when required
local test tools are blocked by `workspace-write`, pass
`--dangerously-bypass-approvals-and-sandbox` and enforce file boundaries
through the prompt contract and parent verification.


Self-healing and schema recovery
--------------------------------

When a CLI upgrade changes RPC method names or payload shapes, dump the current
protocol schemas without spending tokens:

~~~~ bash
codex app-server generate-json-schema -o <temp-dir>
~~~~

Inspect the generated JSON Schema files in `<temp-dir>` or run
`codex debug models` and `codex exec --help` to recover the updated method or
flag names, then update the version-keyed recipe cache in
`$SKILL_CACHE_DIR/recipes.json`.


Execution postures used by `scripts/run.py`
--------------------------------------------

Codex has no CLI switch that disables tools. `--no-tools` therefore runs under
`--sandbox read-only` and relies on the prompt; `run.py` prints a warning and
records `tools_enforced: false` in `meta.json`. Use Claude Code or OpenCode when
tool use must be impossible.

`--isolate` passes `--ignore-rules` so execpolicy `.rules` files from the user
or project are not loaded. Codex still reads repository files under the
read-only sandbox, so a reviewer can inspect the checkout.
