#!/bin/bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN=""
for candidate in .venv/bin/python /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "Python was not found."
  read -r -p "Press Enter to close..."
  exit 1
fi

set +e
"$PYTHON_BIN" stop.py
TASK_EXIT_CODE=$?
set -e
read -r -p "Press Enter to close..."
exit "$TASK_EXIT_CODE"
