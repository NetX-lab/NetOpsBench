#!/bin/bash
# find_python.sh — locate the project Python 3.11+ interpreter.
#
# Source this file after REPO_ROOT or BASE_DIR is set.
# Sets $PYTHON to the best available interpreter using this order:
#
#   1. $NETOPSBENCH_PYTHON  — explicit override via environment variable
#   2. <repo>/.venv/bin/python3        — standard venv name
#   3. <repo>/.venv3*/bin/python3      — version-specific venvs, highest wins
#   4. python3                          — system / active virtualenv fallback

_nb_find_python() {
    local base="${REPO_ROOT:-${BASE_DIR:-}}"

    # 1. Explicit override
    if [ -n "${NETOPSBENCH_PYTHON:-}" ] && [ -x "${NETOPSBENCH_PYTHON}" ]; then
        printf '%s' "${NETOPSBENCH_PYTHON}"; return
    fi

    # 2. Standard .venv
    if [ -x "${base}/.venv/bin/python3" ]; then
        printf '%s' "${base}/.venv/bin/python3"; return
    fi

    # 3. Version-specific venvs (.venv311, .venv312, .venv313 …) — pick highest.
    #    The glob expands in locale sort order; last match = highest version.
    #    If the glob has no matches bash leaves the literal string, which is
    #    not executable, so the check is safe.
    local p latest=""
    for p in "${base}"/.venv3*/bin/python3; do
        [ -x "$p" ] && latest="$p"
    done
    [ -n "$latest" ] && { printf '%s' "$latest"; return; }

    # 4. Fall back to system python3
    printf 'python3'
}

PYTHON="$(_nb_find_python)"
