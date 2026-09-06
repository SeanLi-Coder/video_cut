#!/bin/bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN=""
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ] \
    && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [ -z "$PYTHON_BIN" ] && command -v brew >/dev/null 2>&1; then
  echo "Python 3.10+ is missing. Installing it with Homebrew..."
  set +e
  brew install python
  BREW_EXIT_CODE=$?
  set -e
  if [ "$BREW_EXIT_CODE" -eq 0 ]; then
    PYTHON_BIN="$(brew --prefix)/bin/python3"
  fi
fi

if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
  echo "Python 3.10 or newer is required. Install Homebrew from https://brew.sh and run again."
  read -r -p "Press Enter to close..."
  exit 1
fi

echo "Starting Local Video Cutter..."
set +e
"$PYTHON_BIN" launcher.py
TASK_EXIT_CODE=$?
set -e

if [ "$TASK_EXIT_CODE" -ne 0 ]; then
  echo ""
  read -r -p "Startup stopped. Review the message above, then press Enter to close..."
fi
exit "$TASK_EXIT_CODE"
