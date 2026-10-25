#!/usr/bin/env bash
# First-pass OpenCode review runner for the code-review-loop skill.
#
# Usage:
#   opencode-review.sh models
#       Print the usable review models, one per line, in preference order.
#       Exit 3, with a one-line reason on stderr, when the stage is
#       unavailable.
#   opencode-review.sh run MODEL ROUND [SESSION_ID]
#       Run one review round with protected inputs with $WORK/opencode-prompt.txt as
#       the prompt, resuming SESSION_ID when given, then classify it.
#   opencode-review.sh check PREFIX MODEL
#       Classify the saved output of a round (PREFIX.jsonl, PREFIX.err,
#       PREFIX.exit, PREFIX.export.json) without calling any model.
#
# Environment: REPO_ROOT and WORK are required for `run`; OC_TIMEOUT
# overrides the per-attempt time limit in seconds (default 900).
#
# `run` and `check` print one line, STATUS=<status> MODEL=<id>
# SESSION=<id>, followed by any REASON= and DENIED= lines, and exit with:
#    0  clean        the final answer is exactly NO ACTIONABLE FINDINGS
#   10  findings     a complete review with findings in PREFIX.review.txt
#   20  failed       the reviewer ran but the round is not a usable review
#   21  blocked      a complete answer, but a tool call was denied
#   30  unavailable  known auth, unknown-model or exhausted-quota error
#   31  transient    retryable provider/transport error or empty final reply
#   40  mutated      the repository changed during the round; stop
#   41  guard_failed protected inputs could not be verified; stop
set -uo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
GUARD="$SCRIPT_DIR/review-guard.py"
PRIVATE="$SCRIPT_DIR/opencode-private.py"

PREFERRED_MODELS=(deepseek/deepseek-flash opencode-go/deepseek-v4.1-flash)
AGENT=review-loop-flash
SENTINEL="NO ACTIONABLE FINDINGS"
# Renders an OpenCode error object as "Name [HTTP status]: message".
ERROR_JQ='(.type // .name // "error")
  + (if (.status // .data.statusCode) then " \(.status // .data.statusCode)" else "" end)
  + ": " + ((.data.message // .message // .) | tostring)'

# V2 reads configuration in the server process. The private runner owns a
# loopback server and passes --server so the user's daemon is never used.
OC_ENV=(OPENCODE_CONFIG_PROJECT_DISABLE=1 OPENCODE_DISABLE_FILEWATCHER=1
  OPENCODE_DISABLE_FFF=1 GIT_OPTIONAL_LOCKS=0)

die() { echo "opencode-review: $*" >&2; exit 2; }

require_v2() {
  local version
  if ! command -v opencode >/dev/null 2>&1; then
    echo "unavailable: opencode is not installed" >&2
    return 3
  fi
  OC_BIN=$(command -v opencode)
  # A mise shim can select a different version after chdir into the repository.
  # Resolve it now without installing/upgrading anything.
  if [[ $OC_BIN == */shims/opencode ]] && command -v mise >/dev/null 2>&1; then
    OC_BIN=$(mise which opencode 2>/dev/null) || OC_BIN=""
  fi
  version=$("$OC_BIN" --version 2>/dev/null) || version=""
  # Accept both `2.1.3` and `opencode v2.1.3`; reject unknown/dev labels.
  if [[ ! $version =~ ^(opencode[[:space:]]+)?v?([0-9]+)\.[0-9]+\.[0-9]+([-+][[:alnum:].-]+)?$ ]]; then
    echo "WARNING: cannot verify OpenCode v2 compatibility ($version); upgrade to OpenCode v2 or later. Skipping OpenCode." >&2
    return 3
  fi
  if (( 10#${BASH_REMATCH[2]} < 2 )); then
    echo "WARNING: OpenCode $version is below v2; upgrade to OpenCode v2 or later. Skipping OpenCode." >&2
    return 3
  fi
}

# Keep saved auth/database paths, but exclude global configuration/extensions.
# Custom providers defined only in global config are unavailable in this mode.
prepare_env() {
  mkdir -p -- "$WORK/config" || die "cannot create private config directory"
  chmod 700 -- "$WORK" "$WORK/config" || die "cannot make artifact directory private"
  OC_ENV+=(OPENCODE_CONFIG_DIR="$WORK/config" OPENCODE_CONFIG=)
}

base_config() {
  jq -n '{
    share: "disabled", update: "disable", snapshots: false,
    plugins: ["-opencode.config.instruction", "-opencode.config.skill"],
    policies: [
      {action: "integration.use", resource: "plugin:*", effect: "deny"},
      {action: "integration.use", resource: "mcp:*", effect: "deny"},
      {action: "integration.use", resource: "skill:*", effect: "deny"}
    ]
  }'
}

cmd_models() {
  require_v2 || exit 3
  local listed found=0 m private
  private=$(mktemp -d -t opencode-models.XXXXXX) || die "cannot create private directory"
  # Model discovery needs no session and leaves no configuration behind.
  base_config > "$private/opencode.json"
  listed=$(timeout --kill-after=10 150 python3 "$PRIVATE" \
    --directory "$PWD" --config "$private/opencode.json" \
    --diagnostics "$private/discovery" \
    --cli-json "$(jq -nc --arg cli "$OC_BIN" '[$cli]')" -- models --server @SERVER@ 2>/dev/null)
  local result=$?
  rm -rf -- "$private"
  if [ "$result" != 0 ]; then
    echo "unavailable: opencode models failed" >&2
    exit 3
  fi
  for m in "${PREFERRED_MODELS[@]}"; do
    if grep -Fxq -- "$m" <<<"$listed"; then
      echo "$m"
      found=1
    fi
  done
  if [ "$found" = 0 ]; then
    echo "unavailable: none of ${PREFERRED_MODELS[*]} is configured" >&2
    exit 3
  fi
}

write_config() {
  local data
  prepare_env
  data=$("$OC_BIN" debug paths 2> "$WORK/opencode-paths.err" | sed -n 's/^data  *//p')
  [ -n "$data" ] || die "cannot resolve the OpenCode data directory"
  base_config | jq --arg tool_output "$data/tool-output/*" '. + {
    agents: {
      "review-loop-flash": {
        description: "First-pass code reviewer with protected inputs",
        mode: "primary",
        permissions: [
          {action: "*", resource: "*", effect: "deny"},
          {action: "read", resource: "*", effect: "allow"},
          {action: "read", resource: "*.env", effect: "deny"},
          {action: "read", resource: "*.env.*", effect: "deny"},
          {action: "glob", resource: "*", effect: "allow"},
          {action: "grep", resource: "*", effect: "allow"},
          {action: "external_directory", resource: "*", effect: "deny"},
          {action: "external_directory", resource: $tool_output, effect: "allow"},
          {action: "shell", resource: "*", effect: "allow"}
        ]
      }
    }
  }' > "$WORK/opencode.json" || die "cannot write $WORK/opencode.json"

}


cmd_run() {
  local model=${1:?model} round=${2:?round} session=${3:-}
  if ! require_v2; then
    echo "STATUS=unavailable MODEL=$model SESSION=${session:-unknown}"
    echo "REASON=OpenCode v2 or later is required; skip this pre-stage"
    exit 30
  fi
  : "${REPO_ROOT:?REPO_ROOT is required}" "${WORK:?WORK is required}"
  [ -s "$WORK/opencode-prompt.txt" ] \
    || die "missing $WORK/opencode-prompt.txt"
  local prefix run_dir code sid attempt=1
  run_dir=$(mktemp -d "$WORK/opencode-${model//\//-}-r$round.XXXXXX") || die "cannot create attempt directory"
  local -a resume=()
  [ -n "$session" ] && resume=(--session "$session")
  write_config
  "$OC_BIN" --version > "$WORK/opencode-version.txt" 2>&1
  while :; do
    prefix="$run_dir/a$attempt"
    python3 "$GUARD" --root "$REPO_ROOT" --prefix "$prefix" \
      --stdin "$WORK/opencode-prompt.txt" -- \
      timeout --kill-after=10 "${OC_TIMEOUT:-900}" python3 "$PRIVATE" \
        --directory "$REPO_ROOT" --config "$WORK/opencode.json" --agent "$AGENT" \
        --diagnostics "$prefix" \
        --cli-json "$(jq -nc --arg cli "$OC_BIN" '[$cli]')" -- run --server @SERVER@ \
        --agent "$AGENT" --model "$model#high" \
        --format json "${resume[@]}"
    code=$?
    # Keep saved artifacts available to the offline check subcommand.
    [ ! -f "$prefix.stdout" ] || cp "$prefix.stdout" "$prefix.jsonl"
    [ ! -f "$prefix.stderr" ] || cp "$prefix.stderr" "$prefix.err"
    if [ -f "$prefix.guard.json" ] && jq -e '.status == "mutated"' "$prefix.guard.json" >/dev/null; then
      echo "STATUS=mutated MODEL=$model SESSION=${session:-unknown}"
      echo "REASON=protected inputs changed; preserve $prefix.* and stop"
      exit 40
    fi
    if [ ! -f "$prefix.guard.json" ]; then
      echo "STATUS=guard_failed MODEL=$model SESSION=${session:-unknown}"
      echo "REASON=guard could not verify inputs; preserve $prefix.*"
      exit 41
    fi
    sid=$(jq -r 'select(.sessionID != null) | .sessionID' "$prefix.jsonl" 2>/dev/null | head -1)
    [ -n "$sid" ] || sid=$session
    if [ -n "$sid" ]; then
      (cd "$REPO_ROOT" && env "${OC_ENV[@]}" OPENCODE_CONFIG_CONTENT="$(cat "$WORK/opencode.json")" \
        timeout --kill-after=10 60 "$OC_BIN" session export --standalone "$sid") \
        > "$prefix.export.json" 2> "$prefix.export.err"
    fi
    (cmd_check "$prefix" "$model" "$sid") > "$prefix.status"
    code=$?
    cat "$prefix.status"
    echo "LOG_PREFIX=$prefix"
    if [ "$code" != 31 ] || [ "$attempt" = 2 ]; then
      exit "$code"
    fi
    echo "REASON=retrying transient failure once in a fresh session; logs at $prefix"
    attempt=2
    resume=()
    session=""
  done

}

cmd_check() {
  local prefix=${1:?prefix} model=${2:?model} sid=${3:-}
  local code reasons="" denied="" status exit_code
  local provider=${model%%/*} model_id=${model#*/}
  code=$(cat "$prefix.exit" 2>/dev/null || echo missing)
  if [ -z "$sid" ]; then
    sid=$(jq -r 'select(.sessionID != null) | .sessionID' "$prefix.jsonl" \
      2>/dev/null | head -1)
  fi
  add() { reasons+="REASON=$*"$'\n'; }

  jq -e -s 'length > 0 and all(.[]; type == "object")' "$prefix.jsonl" >/dev/null 2>&1 \
    || add "invalid or empty JSON event stream"

  case $code in
    0) ;;
    124) add "timed out (timeout exited with status 124)" ;;
    *) add "opencode exited with status $code" ;;
  esac
  while IFS= read -r line; do
    [ -n "$line" ] && add "error event: $line"
  done < <(jq -r "select(.type == \"error\") | .error | $ERROR_JQ" \
    "$prefix.jsonl" 2>/dev/null)
  [ -n "$sid" ] || add "no session id in the event stream"

  local export="$prefix.export.json" round_json
  round_json=$(jq -c '
      (.data // .) | .messages as $m
      | ([range(0; $m | length) | select($m[.].type == "user")]
         | last) as $u
      | if $u == null then null else $m[($u + 1):] end' \
    "$export" 2>/dev/null)
  if [ -z "$round_json" ] || [ "$round_json" = null ]; then
    add "no session export with a user message"
    round_json='[]'
  fi

  local n_assistant
  n_assistant=$(jq '[.[] | select(.type == "assistant")] | length' \
    <<<"$round_json")
  [ "$n_assistant" -gt 0 ] || add "no assistant message in this round"

  while IFS= read -r line; do
    [ -n "$line" ] && add "model mismatch: expected $model, got $line"
  done < <(jq -r --arg p "$provider" --arg id "$model_id" --arg a "$AGENT" '
      .[] | select(.type == "assistant")
      | select(.model.providerID != $p or .model.id != $id
               or .model.variant != "high" or .agent != $a)
      | "\(.model.providerID)/\(.model.id) variant=\(.model.variant)"
        + " agent=\(.agent)"' <<<"$round_json" | sort -u)

  while IFS= read -r line; do
    [ -n "$line" ] && add "assistant error: $line"
  done < <(jq -r ".[] | select(.type == \"assistant\" and .error)
      | .error | $ERROR_JQ" <<<"$round_json")

  if ! jq -e 'all(.[] | select(.type == "assistant"); .time.completed != null)' <<<"$round_json" >/dev/null; then
    add "assistant message is incomplete"
  fi

  local finish
  finish=$(jq -r '[.[] | select(.type == "assistant")] | last
      | .finish // "none"' <<<"$round_json")
  if [ "$n_assistant" -gt 0 ] && [ "$finish" != stop ]; then
    add "final step finished with '$finish', not 'stop'"
  fi

  jq -r '[.[] | select(.type == "assistant")] | last
      | [(.content // [])[] | select(.type == "text") | .text] | join("\n")' \
    <<<"$round_json" > "$prefix.review.txt"
  local text
  text=$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
    "$prefix.review.txt" | sed -e '/./,$!d')
  if [ "$n_assistant" -gt 0 ] && [ -z "$text" ]; then
    add "empty final answer (reasoning-only or truncated reply)"
  fi

  denied=$(jq -r '.[] | select(.type == "assistant") | .content[]
      | select(.type == "tool" and .state.status == "error")
      | select(((.state.error // "") | tostring)
          | test("rule which prevents|permission|denied"; "i"))
      | "DENIED=\(.name): \((.state.input.command // .state.input.filePath
          // .state.input.pattern // .state.input.path // "") | tostring)"' \
    <<<"$round_json")

  if [ -n "$reasons" ]; then
    # A missing export or local CLI failure is not provider unavailability.
    # Retry only known transient errors or a completed, empty assistant reply.
    local diagnostics="$reasons $(cat "$prefix.err" 2>/dev/null)"
    if grep -Eq 'invalid or empty JSON event stream|model mismatch|final step finished with.*length' <<<"$reasons"; then
      status=failed exit_code=20
    elif grep -Eiq '401|403|unknown model|model not found|insufficient.quota|quota.exceeded' <<<"$diagnostics"; then
      status=unavailable exit_code=30
    elif grep -Eiq '429|500|502|503|504|ECONNRESET|ETIMEDOUT|connection reset|timed out|empty final answer' <<<"$diagnostics"; then
      status=transient exit_code=31
    else
      status=failed exit_code=20
    fi
  elif [ -n "$denied" ]; then
    status=blocked exit_code=21
  elif [ "$text" = "$SENTINEL" ]; then
    status=clean exit_code=0
  else
    status=findings exit_code=10
  fi
  echo "STATUS=$status MODEL=$model SESSION=${sid:-unknown}"
  printf '%s' "$reasons"
  [ -n "$denied" ] && printf '%s\n' "$denied"
  exit "$exit_code"
}

case ${1:-} in
  models) shift; cmd_models "$@" ;;
  run) shift; cmd_run "$@" ;;
  check) shift; cmd_check "$@" ;;
  *) sed -n '2,27p' "$0"; exit 2 ;;
esac
