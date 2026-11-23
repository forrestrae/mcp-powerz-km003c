# km003c-mcp

This is a fork of `fejesd/mcp-powerz-km003c`: a Python MCP server (stdio) for the ChargerLab POWER-Z KM003C USB-C power meter. It exposes measurements, VBUS/IBUS streaming, USB PD capture with decoding, and the meter's protocol trigger as tools. The README lists the tools and environment variables.

Remotes: `origin` is the fork (`forrestrae/mcp-powerz-km003c`), `upstream` is `fejesd/mcp-powerz-km003c` with push disabled.

## Layout and tests

- `src/km003c_mcp/`: USB layer, protocol and AES framing, PD decoder, recorder, trigger, and `server.py` (entry point `km003c-mcp`).
- `tests/`: unit tests with a fake device in `tests/fakes.py`. `tests/test_hw.py` needs a real meter and carries the `hw` marker.
- `tests/test_workflow_hooks.py`: tests for the git hooks and the commit-subject checker.

```bash
uv sync --extra dev
```

```bash
uv run pytest -m "not hw"
```

```bash
uv run ruff check
```

Hardware tests run only with `KM003C_HW=1` and a meter attached. Ruff is configured to the pyflakes and pycodestyle error subset, because the default rule set flags upstream code that this fork does not rewrite.

## Git workflow

Every change follows this workflow. Hooks enforce part of it on the local machine.

### One-time setup per clone

The human runs this once in each clone. It sets `core.hooksPath` to `.githooks`, makes plain `git merge` on `main` create a merge commit, makes `git pull` fast-forward only, and marks the hooks executable.

```bash
scripts/setup_hooks.sh
```

### Branches

1. Work on a topic branch, never on `main`. Name it `<type>/<kebab-topic>`, where type is one of `feat fix docs refactor test chore perf build ci`.
2. A change meant for upstream goes on a branch cut from `upstream/main` and named `upstream/<topic>`. It holds only that change, so it stays a clean pull-request head. It merges into the fork's `main` like any other branch.

### Commits

- Subjects are Conventional Commits, `type(scope)!: subject`, at most 100 characters, imperative mood. Scope and `!` are optional. `Merge branch '<branch>'` and git's default `Revert "<subject>"` are also accepted. The `commit-msg` hook enforces this through `scripts/check_commits.py`.
- One logical change per commit.
- No attribution trailers.

### Merging

1. Rebase the branch onto `main`, commit everything, and run `scripts/verify.sh` in the worktree. It refuses a dirty tree, untracked files included, then runs `uv run pytest -m "not hw"` and `uv run ruff check`.
2. `main` moves only by `git merge --no-ff <branch> -m "Merge branch '<branch>'"`, run in the checkout that has `main`.
3. The human pushes `main` and tags releases `vX.Y.Z`. Consuming projects pin those tags.

Agents never push. The `pre-push` hook refuses every push when `CLAUDECODE`, `CODEX_THREAD_ID`, `CODEX_SESSION_ID`, `CODEX_TOOL_CALL_ID`, `CODEX_SANDBOX`, or `WORKFLOW_AGENT=1` is set. Agents also do not tag, bypass hooks with `--no-verify`, or change the hook configuration.

### Worktree layout

The clone at `lib/km003c-mcp` stays on `main`; consuming projects run the server from it. Topic work happens in the sibling worktree `lib/km003c-mcp-work`. From the project root, create it with:

```bash
git -C lib/km003c-mcp worktree add ../km003c-mcp-work -b <branch> main
```

For an upstream branch, replace `main` with `upstream/main` and name the branch `upstream/<topic>`.
