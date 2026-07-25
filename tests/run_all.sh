#!/usr/bin/env bash
#
# Desktop test harness for the KivyMD app in app/main.py.
# Runs the KivyMD linter and the headless pytest suite; exits non-zero on any
# failure.  Meant to be run before every push -- it catches in seconds the
# class of errors that otherwise costs a 30-minute Buildozer build.
#
# Usage (from anywhere):
#     bash tests/run_all.sh
#     bash tests/run_all.sh -k termux        # extra args go to pytest
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Kivy must never see pytest's argv, and its default log mode swallows stderr.
export KIVY_NO_ARGS=1
export KIVY_LOG_MODE=PYTHON
export KIVY_GL_BACKEND=mock
export KIVY_AUDIO=mock
export PYTHONPATH="${REPO_ROOT}/tests:${PYTHONPATH:-}"

PY="${PYTHON:-python3}"

# Kivy needs a window to instantiate widgets.  Prefer a real (virtual) X
# display; tests/harness.py falls back to a GL-free mock window when xvfb is
# unavailable, so the suite still runs either way.
if command -v xvfb-run >/dev/null 2>&1; then
    RUNNER=(xvfb-run -a -s "-screen 0 1024x768x24")
else
    echo "note: xvfb-run not found -- using the harness mock window" >&2
    RUNNER=()
fi

if ! "${PY}" -c "import pytest" >/dev/null 2>&1; then
    echo "pytest is not installed; installing..." >&2
    "${PY}" -m pip install --quiet pytest || {
        echo "FAILED: could not install pytest" >&2
        exit 1
    }
fi

status=0

echo "=============================================================="
echo " 1/2  lint_kivymd  (KivyMD 2.x widgets, unknown names, pyjnius)"
echo "=============================================================="
"${RUNNER[@]}" "${PY}" "${SCRIPT_DIR}/lint_kivymd.py" 2>/dev/null
lint_status=$?
if [ "${lint_status}" -ne 0 ]; then
    echo ">> LINT FAILED"
    status=1
else
    echo ">> lint OK"
fi

echo
echo "=============================================================="
echo " 2/2  pytest  (headless app build, ids, handlers, intents)"
echo "=============================================================="
"${RUNNER[@]}" "${PY}" -m pytest "${SCRIPT_DIR}" -q -p no:cacheprovider "$@"
pytest_status=$?
if [ "${pytest_status}" -ne 0 ]; then
    echo ">> PYTEST FAILED"
    status=1
fi

echo
if [ "${status}" -eq 0 ]; then
    echo "ALL CHECKS PASSED"
else
    echo "CHECKS FAILED"
fi
exit "${status}"
