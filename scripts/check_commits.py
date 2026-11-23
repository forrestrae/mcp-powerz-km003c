#!/usr/bin/env python3
"""Lint commit subjects against the repository convention (standard library only).

Usage:
  check_commits.py msg-file <path>   lint one message file (commit-msg hook)
  check_commits.py subject <text>    lint one subject line
  check_commits.py range <rev-range> lint every commit subject in a range

Accepted subjects:
  type(scope)!: subject   scope and ! optional; type is one of TYPES
  Merge branch '<branch>' git's default merge subject
  Revert "<subject>"      git's default revert subject

A subject is at most 100 characters. Exits 1 and lists each offender on failure.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

TYPES = ("feat", "fix", "docs", "refactor", "test", "chore", "perf", "build", "ci")
MAX_LEN = 100

_CONVENTIONAL = re.compile(
    r"^(?:" + "|".join(TYPES) + r")(?:\([a-z0-9][a-z0-9._/-]*\))?!?: \S.*$"
)
_MERGE = re.compile(r"^Merge branch '[^']+'")
_REVERT = re.compile(r'^Revert ".+"$')


def check_subject(subject: str) -> str | None:
    """Return an error message, or None when the subject is acceptable."""
    if not subject.strip():
        return "empty commit subject"
    if len(subject) > MAX_LEN:
        return f"subject is {len(subject)} characters, the limit is {MAX_LEN}: {subject!r}"
    if _MERGE.match(subject) or _REVERT.match(subject):
        return None
    if _CONVENTIONAL.match(subject):
        return None
    return (
        f"subject does not match 'type(scope)!: subject' with type in "
        f"{', '.join(TYPES)}: {subject!r}"
    )


def subject_of(message: str, comment_char: str = "#") -> str:
    """First non-comment, non-blank line of a commit message file."""
    for line in message.splitlines():
        if line.startswith(comment_char) or not line.strip():
            continue
        return line.rstrip()
    return ""


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("msg-file", "subject", "range"):
        print(__doc__, file=sys.stderr)
        return 2
    mode, arg = argv[1], argv[2]
    errors: list[str] = []
    if mode == "msg-file":
        err = check_subject(subject_of(Path(arg).read_text()))
        if err:
            errors.append(err)
    elif mode == "subject":
        err = check_subject(arg)
        if err:
            errors.append(err)
    else:
        out = subprocess.run(
            ["git", "log", "--format=%h %s", arg],
            check=True, capture_output=True, text=True,
        ).stdout
        for line in out.splitlines():
            sha, _, subject = line.partition(" ")
            err = check_subject(subject)
            if err:
                errors.append(f"{sha}: {err}")
    for err in errors:
        print(f"check_commits: {err}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
