#!/bin/sh
# Optional launcher. Committed .mcp.json stays command python3.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SCRIPT="$SCRIPT_DIR/hextile_mcp.py"
if command -v python >/dev/null 2>&1; then
  exec python "$SCRIPT" "$@"
fi
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$SCRIPT" "$@"
fi
exec py -3 "$SCRIPT" "$@"
