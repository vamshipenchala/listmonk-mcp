"""The commit/push guard. Uses fake secrets in a temp dir; never real ones."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("secret_scan", ROOT / "scripts" / "secret_scan.py")
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)

READ = "abcdefghijklmnopqrstuvwxyz012345"
SEND = "SENDsendSENDsendSENDsendSENDsend"
PATH_SECRET = "Zx9_k2LmQ8vR4tYb7NcP1wHs6JdF3gUe5aTq0oIi"


@pytest.fixture
def secrets_dir(tmp_path, monkeypatch):
    d = tmp_path / "listmonk"
    d.mkdir()
    (d / "token").write_text(f"claude_mcp_read:{READ}\n")
    (d / "send-token").write_text(f"claude_cmo_send:{SEND}")
    (d / "connector-url").write_text(f"https://listmonkmcp.example.com/{PATH_SECRET}/mcp\n")
    monkeypatch.setenv("LISTMONK_SECRETS_DIR", str(d))
    return d


def test_loads_secret_values_not_usernames(secrets_dir):
    s = scan.load_secrets()
    assert set(s.values()) == {READ, SEND, PATH_SECRET}


@pytest.mark.parametrize("text,kind", [
    (f"token = '{READ}'", "listmonk read token"),
    (f"x {SEND} y", "listmonk send token"),
    (f"https://host/{PATH_SECRET}/mcp", "MCP URL secret path"),
])
def test_real_secret_values_are_caught(secrets_dir, text, kind):
    findings = scan.scan_text(text, scan.load_secrets())
    assert kind in findings


# Fake secrets are assembled at run time so this file never contains one
# whole -- otherwise the guard would (correctly) block committing it.
@pytest.mark.parametrize("text,kind", [
    ("-----BEGIN RSA " + "PRIVATE KEY-----", "private key"),
    ("-----BEGIN OPENSSH " + "PRIVATE KEY-----", "private key"),
    ("aws = AKIA" + "IOSFODNN7EXAMPLE", "AWS access key"),
    ("LISTMONK_" + "TOKEN=claude_mcp_read:realvalue123456", "filled-in secret variable"),
    ("MCP_PATH_" + "SECRET=Zx9realrealrealrealreal", "filled-in secret variable"),
])
def test_secret_shapes_are_caught_without_known_values(text, kind):
    assert kind in scan.scan_text(text, {})


@pytest.mark.parametrize("text", [
    "LISTMONK_TOKEN=claude_cmo_read:REPLACE_ME",
    "MCP_PATH_SECRET=REPLACE_ME",
    "printf 'API_USER:TOKEN' > ~/.listmonk/token",
    'monkeypatch.setenv("LISTMONK_TOKEN", "justatoken")',
    "ordinary code",
])
def test_placeholders_and_normal_code_pass(text):
    assert scan.scan_text(text, {}) == []


def test_findings_never_contain_the_secret(secrets_dir):
    out = scan.scan_text(f"leak {SEND}", scan.load_secrets())
    assert all(SEND not in f for f in out)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def test_blocks_staged_commit_and_allows_clean_one(secrets_dir, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "ok.txt").write_text("fine\n")
    _git(repo, "add", "ok.txt")
    assert scan.main(["--staged"], cwd=repo) == 0

    (repo / "oops.py").write_text(f"URL = 'https://h/{PATH_SECRET}/mcp'\n")
    _git(repo, "add", "oops.py")
    assert scan.main(["--staged"], cwd=repo) == 1


def test_blocks_push_of_commit_containing_secret(secrets_dir, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("clean\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "clean")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "b.txt").write_text(f"{READ}\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "leak", "--no-verify")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # secret was later deleted, but it's still in history being pushed
    (repo / "b.txt").unlink()
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "remove")
    tip = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert scan.main(["--range", f"{base}..{tip}"], cwd=repo) == 1
    assert scan.main(["--range", f"{tip}..{tip}"], cwd=repo) == 0
