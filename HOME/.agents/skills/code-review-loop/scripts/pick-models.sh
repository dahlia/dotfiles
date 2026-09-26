#!/usr/bin/env bash
# Reviewer model picker for the code-review-loop skill.
#
# Usage:
#   pick-models.sh
#       Read the subscription quota of the connected Codex and Claude Code
#       accounts and pick each reviewer's model family from it:
#
#         quota ample  ->  Codex: newest Astra   Claude: fable (newest Fable)
#         quota tight  ->  Codex: newest Sol     Claude: opus  (newest Opus)
#
# Prints KEY=value lines meant for `eval`:
#   CODEX_MODEL      exact slug, e.g. gpt-6-astra (empty if none resolved)
#   CODEX_QUOTA      ample | tight | unknown
#   CLAUDE_MODEL     fable | opus (moving aliases; read the exact ID back
#                    from modelUsage after the run)
#   CLAUDE_FALLBACK  opus when CLAUDE_MODEL is fable, otherwise empty
#   CLAUDE_QUOTA     ample | tight | unknown
# followed by NOTE= lines that explain each decision for the report.
#
# A window counts as tight when it is at least 80% used, or at least 50%
# used and being spent faster than time is passing (used% > elapsed%), so
# it would run out before it resets. A quota is tight when any of its
# windows is. Unknown quota picks the top tier: the existing fallbacks
# handle a real usage-limit error, and a guessed "tight" would silently
# downgrade every review.
set -uo pipefail

TIGHT_USED=80
PACE_FLOOR=50
NOW=$(date +%s)

NOTES=$(mktemp)
trap 'rm -f "$NOTES"' EXIT
note() { echo "NOTE=$*" >> "$NOTES"; }

# window_tight USED_PERCENT WINDOW_MINUTES RESETS_AT LABEL
# Prints a NOTE and returns 0 when the window is tight.
window_tight() {
  local used=${1%.*} mins=$2 resets=$3 label=$4 elapsed=""
  if [ -n "$mins" ] && [ -n "$resets" ] && [ "$mins" -gt 0 ]; then
    local total=$((mins * 60)) left=$((resets - NOW))
    [ "$left" -lt 0 ] && left=0
    [ "$left" -gt "$total" ] && left=$total
    elapsed=$(( (total - left) * 100 / total ))
  fi
  note "$label: ${used}% used${elapsed:+, ${elapsed}% of window elapsed}"
  [ "$used" -ge "$TIGHT_USED" ] && return 0
  [ -n "$elapsed" ] && [ "$used" -ge "$PACE_FLOOR" ] \
    && [ "$used" -gt "$elapsed" ] && return 0
  return 1
}

# Newest listed Codex model of a family (astra, sol, ...), by version.
codex_newest() {
  codex debug models 2>/dev/null \
    | jq -r --arg fam "$1" '(.models // .)[]
        | select(.visibility == "list")
        | .slug | select(test("^gpt-[0-9.]+-" + $fam + "$"))' \
    | sort -t- -k2,2Vr | head -1
}

codex_quota() {
  local out
  out=$({
    printf '%s\n' \
      '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"code-review-loop","version":"0"}}}' \
      '{"jsonrpc":"2.0","method":"initialized"}' \
      '{"jsonrpc":"2.0","id":2,"method":"account/rateLimits/read"}'
    sleep "${CODEX_PROBE_WAIT:-10}"
  } | timeout $(( ${CODEX_PROBE_WAIT:-10} + 10 )) codex app-server 2>/dev/null \
    | jq -c 'select(.id == 2) | .result' 2>/dev/null | head -1)
  if [ -z "$out" ] || [ "$out" = "null" ]; then
    note "Codex quota: could not read account/rateLimits/read"
    echo unknown
    return
  fi
  local tight=0 w
  for w in primary secondary; do
    local row
    row=$(jq -r --arg w "$w" '.rateLimits[$w]
      | select(. != null)
      | "\(.usedPercent) \(.windowDurationMins // "") \(.resetsAt // "")"' \
      <<<"$out")
    [ -n "$row" ] || continue
    # shellcheck disable=SC2086
    window_tight $row "Codex $w window" && tight=1
  done
  if [ "$(jq -r '.rateLimits.rateLimitReachedType // empty' <<<"$out")" ]; then
    note "Codex quota: a rate limit is already reached"
    tight=1
  fi
  [ "$tight" = 1 ] && echo tight || echo ample
}

claude_quota() {
  local event
  event=$(echo "Reply with OK." | timeout 120 claude -p --model haiku \
      --safe-mode --strict-mcp-config --no-session-persistence \
      --tools "" --output-format stream-json --verbose 2>/dev/null \
    | jq -c 'select(.type == "rate_limit_event") | .rate_limit_info' \
      2>/dev/null | tail -1)
  if [ -z "$event" ] || [ "$event" = "null" ]; then
    note "Claude quota: no rate_limit_event in the probe output"
    echo unknown
    return
  fi
  local tight=0 name
  declare -A mins=([five_hour]=300 [seven_day]=10080)
  for name in $(jq -r '.unifiedWindows // {} | keys[]' <<<"$event"); do
    local used resets
    read -r used resets < <(jq -r --arg n "$name" '.unifiedWindows[$n]
      | "\((.utilization * 100) | floor) \(.resetsAt // "")"' <<<"$event")
    window_tight "$used" "${mins[$name]:-}" "${resets:-}" "Claude $name window" \
      && tight=1
  done
  if [ "$(jq -r '.status' <<<"$event")" != "allowed" ]; then
    note "Claude quota: status is $(jq -r '.status' <<<"$event")"
    tight=1
  fi
  [ "$tight" = 1 ] && echo tight || echo ample
}

# --- Codex -----------------------------------------------------------------
CODEX_QUOTA=$(codex_quota)
if [ "$CODEX_QUOTA" = tight ]; then fam=sol; else fam=astra; fi
CODEX_MODEL=$(codex_newest "$fam")
if [ -z "$CODEX_MODEL" ]; then
  note "Codex: no listed $fam model in \`codex debug models\`"
fi
echo "CODEX_QUOTA=$CODEX_QUOTA"
echo "CODEX_MODEL=$CODEX_MODEL"
note "Codex: quota $CODEX_QUOTA -> newest $fam = ${CODEX_MODEL:-none}"

# --- Claude ----------------------------------------------------------------
CLAUDE_QUOTA=$(claude_quota)
if [ "$CLAUDE_QUOTA" = tight ]; then
  CLAUDE_MODEL=opus CLAUDE_FALLBACK=
else
  CLAUDE_MODEL=fable CLAUDE_FALLBACK=opus
fi
echo "CLAUDE_QUOTA=$CLAUDE_QUOTA"
echo "CLAUDE_MODEL=$CLAUDE_MODEL"
echo "CLAUDE_FALLBACK=$CLAUDE_FALLBACK"
note "Claude: quota $CLAUDE_QUOTA -> $CLAUDE_MODEL"
cat "$NOTES"
