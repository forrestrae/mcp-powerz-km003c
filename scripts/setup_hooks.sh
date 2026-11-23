#!/bin/sh
# One-time setup per clone: activate the repository's git hooks and merge
# settings. See AGENTS.md, "Git workflow".
set -eu

root=$(git rev-parse --show-toplevel)
cd "$root"

chmod +x .githooks/* scripts/*.sh

git config core.hooksPath .githooks
# A plain `git merge <branch>` on main always creates a merge commit.
git config branch.main.mergeoptions --no-ff
# `git pull` only fast-forwards; without this, mergeoptions would turn a pull into a merge.
git config pull.ff only

echo "setup_hooks: hooks active (core.hooksPath=.githooks)"
