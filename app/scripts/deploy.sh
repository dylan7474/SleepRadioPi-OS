#!/usr/bin/env bash
#
# Sync the working tree to the Pi and run the test suite there (and
# optionally the app itself). The "adb install" equivalent for this
# project -- see docs/PI_SETUP.md.
#
# Usage:
#   ./scripts/deploy.sh          # sync + run tests
#   ./scripts/deploy.sh --run    # sync + run sleepradiopi/main.py
#
# Assumes an SSH host alias `sleepradiopi` (see docs/PI_SETUP.md step 3)
# and that `python3 -m venv .venv && pip install -e ".[dev]"` has already
# been run once on the Pi (docs/PI_SETUP.md step 5).

set -euo pipefail

HOST="${SLEEPRADIOPI_HOST:-sleepradiopi}"
REMOTE_DIR="${SLEEPRADIOPI_REMOTE_DIR:-~/SleepRadioPi}"

here="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Syncing to $HOST:$REMOTE_DIR"
rsync -az --delete \
    --exclude ".git" \
    --exclude ".venv" \
    --exclude "__pycache__" \
    --exclude "*.pyc" \
    --exclude ".pytest_cache" \
    --exclude "voices" \
    "$here"/ "$HOST:$REMOTE_DIR"/

if [[ "${1:-}" == "--run" ]]; then
    echo "==> Running sleepradiopi/main.py on $HOST"
    ssh "$HOST" "cd $REMOTE_DIR && .venv/bin/python -m sleepradiopi.main"
else
    echo "==> Running test suite on $HOST"
    ssh "$HOST" "cd $REMOTE_DIR && .venv/bin/python -m pytest -q"
fi
