"""Tests for the git workflow tooling: the commit-subject checker and the pre-push hook.

They run the scripts through subprocess, so the hooks need not be installed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "check_commits.py"
COMMIT_MSG_HOOK = ROOT / ".githooks" / "commit-msg"
PRE_PUSH_HOOK = ROOT / ".githooks" / "pre-push"

AGENT_VARS = (
    "CLAUDECODE",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CODEX_TOOL_CALL_ID",
    "CODEX_SANDBOX",
    "WORKFLOW_AGENT",
)


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in AGENT_VARS}


def _check(subject: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), "subject", subject],
        capture_output=True, text=True,
    )


@pytest.mark.parametrize(
    "subject",
    [
        "feat: decode extended messages with chunk reassembly",
        "fix(recorder): poll 1000 SPS streams every 20 ms",
        "feat(pd)!: rename the decoder entry point",
        "chore: add MIT license",
        "docs: describe the PD decoding coverage",
        "refactor: split the decoder",
        "test: hardware validation against a real KM003C",
        "perf: avoid a copy per sample",
        "build: add ruff to the dev extras",
        "ci: run the unit tests",
        "Merge branch 'chore/fork-workflow'",
        'Revert "feat: decode extended messages"',
        "fix: " + "a" * 95,  # exactly 100 characters
    ],
)
def test_good_subject_accepted(subject):
    assert len(subject) <= 100
    result = _check(subject)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "subject",
    [
        "added a thing",
        "feature: add a thing",
        "feat:missing space",
        "feat: ",
        "Feat: capitalised type",
        "fix(): empty scope",
        "feat: " + "a" * 100,  # over 100 characters
        "",
    ],
)
def test_bad_subject_rejected(subject):
    result = _check(subject)
    assert result.returncode == 1
    assert "check_commits" in result.stderr


def test_commit_msg_hook_rejects_bad_subject(tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("update stuff\n\nbody\n")
    result = subprocess.run(
        [str(COMMIT_MSG_HOOK), str(msg)], capture_output=True, text=True, cwd=ROOT
    )
    assert result.returncode == 1


def test_commit_msg_hook_accepts_good_subject_and_skips_comments(tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("# a comment line\nfeat(scope): add a thing\n\nbody\n")
    result = subprocess.run(
        [str(COMMIT_MSG_HOOK), str(msg)], capture_output=True, text=True, cwd=ROOT
    )
    assert result.returncode == 0, result.stderr


def test_existing_history_style_is_accepted():
    """Every subject on the fork's main since the fork point passes the rule."""
    base = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "4b26a6c"],
        capture_output=True, text=True, cwd=ROOT,
    )
    if base.returncode != 0:
        pytest.skip("fork point 4b26a6c not available")
    result = subprocess.run(
        [sys.executable, str(CHECKER), "range", "4b26a6c"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr


def test_pre_push_allows_a_human_shell():
    result = subprocess.run(
        [str(PRE_PUSH_HOOK), "origin", "git@example.invalid:x/y.git"],
        capture_output=True, text=True, env=_clean_env(), cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "var,value",
    [
        ("CLAUDECODE", "1"),
        ("CODEX_THREAD_ID", "t"),
        ("CODEX_SESSION_ID", "s"),
        ("CODEX_TOOL_CALL_ID", "c"),
        ("CODEX_SANDBOX", "seatbelt"),
        ("WORKFLOW_AGENT", "1"),
    ],
)
def test_pre_push_refused_for_agent_env(var, value):
    env = _clean_env()
    env[var] = value
    result = subprocess.run(
        [str(PRE_PUSH_HOOK), "origin", "git@example.invalid:x/y.git"],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )
    assert result.returncode == 1
    assert var in result.stderr
    assert "Agents never push" in result.stderr


def test_pre_push_refused_with_claudecode_1():
    env = _clean_env()
    env["CLAUDECODE"] = "1"
    result = subprocess.run(
        [str(PRE_PUSH_HOOK)], capture_output=True, text=True, env=env, cwd=ROOT
    )
    assert result.returncode == 1


def test_pre_push_ignores_workflow_agent_not_one():
    env = _clean_env()
    env["WORKFLOW_AGENT"] = "0"
    result = subprocess.run(
        [str(PRE_PUSH_HOOK)], capture_output=True, text=True, env=env, cwd=ROOT
    )
    assert result.returncode == 0
