#!/usr/bin/env bash
set -euo pipefail

: "${SKILL_MANAGER_BIN_DIR:?SKILL_MANAGER_BIN_DIR is required}"
: "${SKILL_MANAGER_CACHE_DIR:?SKILL_MANAGER_CACHE_DIR is required}"
: "${SKILL_DIR:?SKILL_DIR is required}"
: "${SKILL_NAME:=skt}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "skt install requires python3" >&2
  exit 127
fi

# SKILL_DIR is the installed unit's store root. The entrypoint is
# stdlib-only, so it runs on the system python3 with no venv.
ENTRYPOINT="$SKILL_DIR/src/skt/cli.py"
if [[ ! -f "$ENTRYPOINT" ]]; then
  echo "skt entrypoint not found at $ENTRYPOINT" >&2
  exit 1
fi

mkdir -p "$SKILL_MANAGER_BIN_DIR" "$SKILL_MANAGER_CACHE_DIR"

WRAPPER="$SKILL_MANAGER_BIN_DIR/skt"
cat > "$WRAPPER" <<SH
#!/usr/bin/env bash
set -euo pipefail

exec python3 "$ENTRYPOINT" "\$@"
SH
chmod 0755 "$WRAPPER"

"$WRAPPER" --help >/dev/null
echo "installed skt for $SKILL_NAME at $WRAPPER"
