#!/usr/bin/env bash
# First-pass OpenCode review runner for the code-review-loop skill.
#
# Usage:
#   opencode-review.sh models
#       Print the usable review models, one per line, in preference order.
#       Exit 3, with a one-line reason on stderr, when the stage is
#       unavailable.
#   opencode-review.sh run MODEL ROUND [SESSION_ID]
#       Run one read-only review round with $WORK/opencode-prompt.txt as
#       the prompt, resuming SESSION_ID when given, then classify it.
#   opencode-review.sh check PREFIX MODEL
#       Classify the saved output of a round (PREFIX.jsonl, PREFIX.err,
#       PREFIX.exit, PREFIX.export.json) without calling any model.
#
# Environment: REPO_ROOT and WORK are required for `run`; OC_TIMEOUT
# overrides the per-round time limit in seconds (default 900).
#
# `run` and `check` print one line, STATUS=<status> MODEL=<id>
# SESSION=<id>, followed by any REASON= and DENIED= lines, and exit with:
#    0  clean        the final answer is exactly NO ACTIONABLE FINDINGS
#   10  findings     a complete review with findings in PREFIX.review.txt
#   20  failed       the reviewer ran but the round is not a usable review
#   21  blocked      a complete answer, but a tool call was denied
#   30  unavailable  the provider produced no assistant output at all
#                    (auth, unknown model, quota); try the next model
#   40  mutated      the repository changed during the round; stop
set -uo pipefail

PREFERRED_MODELS=(deepseek/deepseek-flash opencode-go/deepseek-v4.1-flash)
AGENT=review-loop-flash
SENTINEL="NO ACTIONABLE FINDINGS"
# Renders an OpenCode error object as "Name [HTTP status]: message".
ERROR_JQ='(.name // "error")
  + (if .data.statusCode then " \(.data.statusCode)" else "" end)
  + ": " + ((.data.message // .message // .) | tostring)'

# Isolate the reviewer from project and user extensions: no project
# opencode.json or .opencode/, no Claude Code prompts or skills, no
# external plugins (--pure), no sharing, no self-update, no LSP
# downloads, and no optional index writes from git status.
OC_ENV=(
  OPENCODE_DISABLE_PROJECT_CONFIG=1
  OPENCODE_DISABLE_CLAUDE_CODE=1
  OPENCODE_DISABLE_EXTERNAL_SKILLS=1
  OPENCODE_DISABLE_AUTOUPDATE=1
  OPENCODE_DISABLE_SHARE=1
  OPENCODE_DISABLE_LSP_DOWNLOAD=1
  GIT_OPTIONAL_LOCKS=0
)

die() { echo "opencode-review: $*" >&2; exit 2; }

cmd_models() {
  if ! command -v opencode >/dev/null 2>&1; then
    echo "unavailable: opencode is not installed" >&2
    exit 3
  fi
  local listed found=0 m
  listed=$(env "${OC_ENV[@]}" opencode models --pure 2>/dev/null) || {
    echo "unavailable: opencode models failed" >&2
    exit 3
  }
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
  local data mcp_off
  data=$(opencode debug paths 2>/dev/null | sed -n 's/^data  *//p')
  [ -n "$data" ] || die "cannot resolve the OpenCode data directory"
  # Disable every MCP server the user's global config would start.  The
  # resolved config holds credentials, so it only ever reaches jq.
  mcp_off=$(cd "$REPO_ROOT" && env "${OC_ENV[@]}" \
    opencode debug config --pure 2>/dev/null \
    | jq -ce '(.mcp // {}) | map_values({enabled: false})') \
    || die "cannot resolve the OpenCode configuration"
  jq -n --arg tool_output "$data/tool-output/*" --argjson mcp "$mcp_off" '{
    "$schema": "https://opencode.ai/config.json",
    share: "disabled",
    autoupdate: false,
    mcp: $mcp,
    agent: {
      "review-loop-flash": {
        description: "Read-only first-pass code reviewer",
        mode: "primary",
        permission: {
          "*": "deny",
          read: {"*": "allow", "*.env": "deny", "*.env.*": "deny"},
          glob: "allow",
          grep: "allow",
          list: "allow",
          todowrite: "allow",
          external_directory: {"*": "deny", ($tool_output): "allow"},
          bash: {
            "*": "deny",
            "git status *": "allow",
            "git diff *": "allow",
            "git log *": "allow",
            "git show *": "allow",
            "git blame *": "allow",
            "git grep *": "allow",
            "git rev-parse *": "allow",
            "git rev-list *": "allow",
            "git merge-base *": "allow",
            "git ls-files *": "allow",
            "git ls-tree *": "allow",
            "git cat-file *": "allow",
            "git describe *": "allow",
            "git shortlog *": "allow",
            "git branch --show-current": "allow",
            "*>*": "deny",
            "git *--output*": "deny",
            "git *--no-index*": "deny",
            "git *--contents*": "deny",
            "git grep *-O*": "deny",
            "git grep *--open-files-in-pager*": "deny"
          }
        }
      }
    }
  }' > "$WORK/opencode.json" || die "cannot write $WORK/opencode.json"
  # Refuse to run unless OpenCode resolves the agent with its guard in
  # force: an unknown or shadowed agent would run with default permissions.
  (cd "$REPO_ROOT" && env "${OC_ENV[@]}" \
    OPENCODE_CONFIG_CONTENT="$(cat "$WORK/opencode.json")" \
    opencode debug agent "$AGENT" --pure 2>/dev/null) \
    | jq -e --arg a "$AGENT" '.name == $a
        and ([.permission[] | select(.permission == "*" and .pattern == "*")]
             | last | .action) == "deny"
        and .tools.edit == false and .tools.write == false
        and .tools.task == false and .tools.webfetch == false' >/dev/null \
    || die "OpenCode did not resolve the read-only $AGENT agent"
}

snapshot() {
  git rev-parse HEAD
  git for-each-ref --format='%(refname) %(objectname)'
  git status --porcelain -uall
  git diff HEAD
  git ls-files -o --exclude-standard -z | sort -z | xargs -0 -r sha256sum
}

cmd_run() {
  local model=${1:?model} round=${2:?round} session=${3:-}
  : "${REPO_ROOT:?REPO_ROOT is required}" "${WORK:?WORK is required}"
  [ -s "$WORK/opencode-prompt.txt" ] \
    || die "missing $WORK/opencode-prompt.txt"
  local prefix="$WORK/opencode-r$round" code sid
  local -a resume=()
  [ -n "$session" ] && resume=(--session "$session")
  write_config
  (cd "$REPO_ROOT" && snapshot) > "$prefix.before" 2>&1
  (
    cd "$REPO_ROOT" &&
    env "${OC_ENV[@]}" \
      OPENCODE_CONFIG_CONTENT="$(cat "$WORK/opencode.json")" \
      timeout "${OC_TIMEOUT:-900}" opencode run --pure \
        --agent "$AGENT" --model "$model" --variant high \
        --format json "${resume[@]}" \
        < "$WORK/opencode-prompt.txt" > "$prefix.jsonl" 2> "$prefix.err"
  )
  code=$?
  echo "$code" > "$prefix.exit"
  (cd "$REPO_ROOT" && snapshot) > "$prefix.after" 2>&1
  if ! cmp -s "$prefix.before" "$prefix.after"; then
    echo "STATUS=mutated MODEL=$model SESSION=${session:-unknown}"
    echo "REASON=repository changed during the review; see" \
      "diff $prefix.before $prefix.after"
    exit 40
  fi
  sid=$(jq -r 'select(.sessionID != null) | .sessionID' "$prefix.jsonl" \
    2>/dev/null | head -1)
  [ -n "$sid" ] || sid=$session
  if [ -n "$sid" ]; then
    (cd "$REPO_ROOT" && env "${OC_ENV[@]}" opencode export --pure "$sid") \
      > "$prefix.export.json" 2> "$prefix.export.err"
  fi
  cmd_check "$prefix" "$model" "$sid"
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
      .messages as $m
      | ([range(0; $m | length) | select($m[.].info.role == "user")]
         | last) as $u
      | if $u == null then null else $m[($u + 1):] end' \
    "$export" 2>/dev/null)
  if [ -z "$round_json" ] || [ "$round_json" = null ]; then
    add "no session export with a user message"
    round_json='[]'
  fi

  local n_assistant n_content
  n_assistant=$(jq '[.[] | select(.info.role == "assistant")] | length' \
    <<<"$round_json")
  # Any assistant part at all (even a step or reasoning part) means the
  # model was reached; only a round with none is a provider failure.
  n_content=$(jq '[.[] | select(.info.role == "assistant") | .parts[]]
    | length' <<<"$round_json")
  [ "$n_assistant" -gt 0 ] || add "no assistant message in this round"

  while IFS= read -r line; do
    [ -n "$line" ] && add "model mismatch: expected $model, got $line"
  done < <(jq -r --arg p "$provider" --arg id "$model_id" --arg a "$AGENT" '
      .[] | select(.info.role == "assistant")
      | select(.info.providerID != $p or .info.modelID != $id
               or .info.variant != "high" or .info.agent != $a)
      | "\(.info.providerID)/\(.info.modelID) variant=\(.info.variant)"
        + " agent=\(.info.agent)"' <<<"$round_json" | sort -u)

  while IFS= read -r line; do
    [ -n "$line" ] && add "assistant error: $line"
  done < <(jq -r ".[] | select(.info.role == \"assistant\" and .info.error)
      | .info.error | $ERROR_JQ" <<<"$round_json")

  local finish
  finish=$(jq -r '[.[] | select(.info.role == "assistant")] | last
      | .info.finish // "none"' <<<"$round_json")
  if [ "$n_assistant" -gt 0 ] && [ "$finish" != stop ]; then
    add "final step finished with '$finish', not 'stop'"
  fi

  jq -r '[.[] | select(.info.role == "assistant")] | last
      | [(.parts // [])[] | select(.type == "text") | .text] | join("\n")' \
    <<<"$round_json" > "$prefix.review.txt"
  local text
  text=$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
    "$prefix.review.txt" | sed -e '/./,$!d')
  if [ "$n_assistant" -gt 0 ] && [ -z "$text" ]; then
    add "empty final answer (reasoning-only or truncated reply)"
  fi

  denied=$(jq -r '.[] | select(.info.role == "assistant") | .parts[]
      | select(.type == "tool" and .state.status == "error")
      | select((.state.error // "")
          | test("rule which prevents|permission|denied"; "i"))
      | "DENIED=\(.tool): \((.state.input.command // .state.input.filePath
          // .state.input.pattern // .state.input.path // "") | tostring)"' \
    <<<"$round_json")

  if [ -n "$reasons" ]; then
    if [ "$n_content" = 0 ] && [ "$code" != 124 ]; then
      status=unavailable exit_code=30
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
