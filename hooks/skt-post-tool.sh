#!/usr/bin/env bash
# PostToolUse hook: throttled staleness check for long-running sessions.
#
# `skt check --cached` reads a state file and touches the network only
# after the TTL (default 15 min) expires, so this is cheap on every tool
# call and pays one real check per window. Exit 10 from skt means
# notifications exist; they are surfaced via hookSpecificOutput
# additionalContext (harnesses that predate that field ignore it — the
# SessionStart injection remains the primary disclosure). NEVER exits
# non-zero, and logs to the hook.log contract only when a notification
# actually fires (one line per session start + one per notification —
# not one per tool call).
set -u

pick_python() {
  if [ -n "${SKT_PYTHON:-}" ]; then printf '%s' "$SKT_PYTHON"; return 0; fi
  for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
    resolved="$(command -v "$candidate" 2>/dev/null)" || continue
    if "$resolved" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      printf '%s' "$resolved"; return 0
    fi
  done
  return 1
}

resolve_skt() {
  if [ -n "${SKILL_MANAGER_HOME:-}" ] && [ -x "$SKILL_MANAGER_HOME/bin/cli/skt" ]; then
    printf '%s' "$SKILL_MANAGER_HOME/bin/cli/skt"; return 0
  fi
  if command -v skt >/dev/null 2>&1; then
    printf '%s' "skt"; return 0
  fi
  if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "$CLAUDE_PLUGIN_ROOT/src/skt/cli.py" ]; then
    py="$(pick_python)" || return 1
    printf '%s %s' "$py" "$CLAUDE_PLUGIN_ROOT/src/skt/cli.py"; return 0
  fi
  return 1
}

SKT_CMD="$(resolve_skt)" || exit 0

NOTES="$($SKT_CMD check --cached 2>/dev/null)"
rc=$?
[ "$rc" -eq 10 ] || exit 0

if [ -n "${SKILL_MANAGER_HOME:-}" ]; then
  logdir="$SKILL_MANAGER_HOME/logs/skt"
  mkdir -p "$logdir" 2>/dev/null && printf '%s post-tool session=%s check-notified\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${CLAUDE_SESSION_ID:-unknown}" \
    >> "$logdir/hook.log" 2>/dev/null || true
fi

ESCAPED=$(printf '%s' "$NOTES" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null) || exit 0
printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":%s}}\n' "$ESCAPED"
exit 0
