#!/bin/sh
# Gate before merging a topic branch into main: a clean tree, the unit tests, and lint.
# Run it from the worktree that holds the branch. See AGENTS.md, "Git workflow".
set -eu

root=$(git rev-parse --show-toplevel)
cd "$root"

if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
    echo "verify: the working tree is not clean (untracked files count). Commit or remove:" >&2
    git status --short --untracked-files=all >&2
    exit 1
fi

echo "verify: uv run --extra dev pytest -m \"not hw\""
uv run --extra dev pytest -m "not hw"

echo "verify: uv run --extra dev ruff check"
uv run --extra dev ruff check

echo "verify: passed on $(git rev-parse --short HEAD)"
