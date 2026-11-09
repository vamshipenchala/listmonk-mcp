#!/usr/bin/env python3
"""Block commits and pushes that contain listmonk credentials or the MCP URL secret.

Run by .githooks/pre-commit (--staged) and .githooks/pre-push (--range A..B).
Enable once per clone:  git config core.hooksPath .githooks

Two kinds of check:
  1. Exact values of YOUR secrets, read at run time from ~/.listmonk/
     (override with LISTMONK_SECRETS_DIR). This file contains no secrets.
  2. Secret shapes that need no known value: private keys, AWS keys,
     and secret variables filled in with something other than REPLACE_ME.

Findings name the file and the kind of secret, never the secret itself.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

MIN_SECRET_LEN = 12

SHAPES = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "filled-in secret variable": re.compile(
        r"\b(?:LISTMONK_TOKEN|LISTMONK_SEND_TOKEN|MCP_PATH_SECRET)=([^\s'\"]{8,})"
    ),
}


def load_secrets() -> dict[str, str]:
    """Map kind -> secret value, from the local secrets directory."""
    d = Path(os.environ.get("LISTMONK_SECRETS_DIR", "~/.listmonk")).expanduser()
    out: dict[str, str] = {}

    def token_value(name: str) -> str | None:
        p = d / name
        if not p.exists():
            return None
        raw = p.read_text().strip()
        return raw.split(":", 1)[1] if ":" in raw else raw

    for kind, name in (("listmonk read token", "token"), ("listmonk send token", "send-token")):
        v = token_value(name)
        if v and len(v) >= MIN_SECRET_LEN:
            out[kind] = v

    url_file = d / "connector-url"
    if url_file.exists():
        parts = url_file.read_text().strip().split("/")
        # https: / '' / host / <secret> / mcp
        if len(parts) >= 5 and len(parts[3]) >= MIN_SECRET_LEN:
            out["MCP URL secret path"] = parts[3]
    return out


def scan_text(text: str, secrets: dict[str, str]) -> list[str]:
    """Return the kinds of secret found in text (never the values)."""
    found = [kind for kind, value in secrets.items() if value in text]
    for kind, rx in SHAPES.items():
        for m in rx.finditer(text):
            if "REPLACE_ME" not in m.group(0):  # templates like deploy/mcp.env.example
                found.append(kind)
                break
    return found


def _git(cwd: Path | None, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _git_bytes(cwd: Path | None, *args: str) -> str:
    raw = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout
    return raw.decode("utf-8", "ignore")


def _staged(cwd: Path | None) -> list[tuple[str, str]]:
    names = _git(cwd, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0")
    return [(n, _git_bytes(cwd, "show", f":{n}")) for n in names if n]


def _range(cwd: Path | None, rev_range: str) -> list[tuple[str, str]]:
    """Every file version and commit message introduced by commits in the range."""
    items: list[tuple[str, str]] = []
    for commit in _git(cwd, "rev-list", rev_range).split():
        items.append((f"commit {commit[:8]} message", _git_bytes(cwd, "log", "-1", "--format=%B", commit)))
        for line in _git(cwd, "diff-tree", "-r", "--no-commit-id", "--root", "--diff-filter=ACMR", commit).splitlines():
            meta, path = line.split("\t", 1)
            blob = meta.split()[3]
            items.append((f"{path} (in {commit[:8]})", _git_bytes(cwd, "cat-file", "-p", blob)))
    return items


def main(argv: list[str], cwd: Path | None = None) -> int:
    if argv[:1] == ["--staged"]:
        items = _staged(cwd)
    elif argv[:1] == ["--range"] and len(argv) == 2:
        items = _range(cwd, argv[1])
    else:
        print("usage: secret_scan.py --staged | --range A..B", file=sys.stderr)
        return 2

    secrets = load_secrets()
    if not secrets:
        print("secret-scan: warning: no local secrets found in ~/.listmonk; "
              "checking secret shapes only.", file=sys.stderr)

    problems = [(where, kind) for where, text in items for kind in scan_text(text, secrets)]
    if not problems:
        return 0
    print("\nBLOCKED: this would publish a secret.\n", file=sys.stderr)
    for where, kind in problems:
        print(f"  {where}: {kind}", file=sys.stderr)
    print("\nRemove it (keep secrets in ~/.listmonk/ or the server's mcp.env) and try again.\n"
          "If it's already in a commit, rewrite that commit before pushing.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
