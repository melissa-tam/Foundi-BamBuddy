#!/bin/sh
# Dev-loop lint + test runner.
#
# Runs the SAME pytest node roots resolved against the SAME config as the ship
# gate (foundi-ops ship-manifest.psd1, PytestBaseline step:
#   .venv\Scripts\python.exe -m pytest backend/tests tests -p no:cacheprovider -q -rfE
# with Cwd = repo root), so "green here" means the gate saw the same test set.
# The only deliberate differences are parallelism (-n; the gate runs serial) and
# verbosity (-v instead of the -q -rfE the gate's baseline parser needs).
#
# Both roots are named on purpose: the suite spans backend/tests AND the
# repo-root tests/ directory, and pyproject.toml's testpaths lists only the
# former -- so a dev loop that trusts testpaths never collects tests/ and never
# sees a breakage the gate will stop on.
#
# There is no --full flag any more: the full suite is always run. It used to
# skip tests/unit/services/test_bambu_ftp.py, which is the only coverage of
# bambu_ftp.cleanup_downloaded_3mf -- THE deletion gate for downloaded 3MFs.
# That is a data-loss guard and it belongs in the dev loop, not just the gate.
#
# Usage:
#   ./test_backend.sh                      lint, then the full suite
#   ./test_backend.sh -k some_test         extra args are forwarded to pytest
#   ./test_backend.sh backend/tests/unit   ...including explicit node ids
#   PYTEST_XDIST_N=8 ./test_backend.sh     override the worker count
set -e

# Resolve the repo root from this script's own location, so the script behaves
# the same from any CWD and pytest picks up the root pyproject.toml as its
# config -- the same rootdir the ship gate resolves.
REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$REPO_ROOT"

# Interpreter: Git Bash on Windows has .venv/Scripts/python.exe, Linux and the
# Docker image have .venv/bin/python. Detect, never hardcode one.
if [ -f ".venv/Scripts/python.exe" ]; then
    PY="$REPO_ROOT/.venv/Scripts/python.exe"
elif [ -f ".venv/bin/python" ]; then
    PY="$REPO_ROOT/.venv/bin/python"
else
    echo "test_backend.sh: no virtualenv interpreter found in $REPO_ROOT" >&2
    echo "  looked for .venv/Scripts/python.exe (Windows) and .venv/bin/python (Linux)" >&2
    exit 1
fi

# Lint. ruff is installed into the venv and is NOT on PATH, so it is invoked
# through the same interpreter. Kept scoped to backend/ as before; the root
# pyproject.toml supplies [tool.ruff] either way, since ruff resolves config
# by walking up from the files it checks.
(
    cd backend
    "$PY" -m ruff check
    "$PY" -m ruff format --check
)

# Test.
"$PY" -m pytest backend/tests tests -v -n "${PYTEST_XDIST_N:-30}" "$@"
