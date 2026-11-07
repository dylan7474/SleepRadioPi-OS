#!/usr/bin/env bash
#
# Run the tests.
#
#   scripts/test.sh            all of them, in parallel if pytest-xdist is installed (about 1.5 minutes on 4
#                              cores instead of 3.5): before a commit that matters, and ALWAYS before a release
#   scripts/test.sh quick      only the test files for what has changed since the last commit (seconds): for
#                              working; it's a guide, not a gate
#   scripts/test.sh ARGS...    anything else goes to pytest (a file, -k name, -x ...)
#
# pytest-xdist: `paru -S python-pytest-xdist` (Arch), or `pip install pytest-xdist` in the venv. Without it
# everything still runs, one test at a time. PYTHON=... chooses the interpreter.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python}

if [ "${1:-}" = quick ]; then
	shift
	mapfile -t files < <("$PY" scripts/tests_for_changes.py)
	if [ ${#files[@]} -eq 0 ]; then
		echo "quick: no test file matches what has changed (nothing changed, or only docs): run scripts/test.sh for all of them" >&2
		exit 0
	fi
	echo "quick: ${#files[@]} test file(s) for the changes: ${files[*]}" >&2
	exec "$PY" -m pytest -q "$@" "${files[@]}"
fi

par=()
if [ $# -eq 0 ] && "$PY" -c "import xdist" 2>/dev/null; then
	par=(-n auto)
fi
exec "$PY" -m pytest -q "${par[@]}" "$@"
