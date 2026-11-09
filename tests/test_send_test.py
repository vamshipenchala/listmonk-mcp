"""send_test: sends the SAVED campaign, only to allowlisted test addresses."""

from __future__ import annotations

import asyncio
import copy

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from listmonk_mcp import server

ALLOWED = "tester.one@example.com, Tester.Two@example.org"
CAMPAIGN = {
    "id": 6, "name": "Draft", "subject": "Hello", "body": "<p>Saved body</p>",
    "content_type": "html", "from_email": "Destiny <support@example.com>",
    "template_id": 5, "type": "regular", "status": "draft", "messenger": "email",
    "tags": [], "headers": [], "lists": [{"id": 7, "name": "Test"}],
}


class Fake:
    def __init__(self):
        self.campaign = copy.deepcopy(CAMPAIGN)
        self.calls = []

    def __call__(self, method, path, token=None, **kw):
        self.calls.append((method, path, token, kw.get("json")))
        if method == "GET" and path == "/api/campaigns/6":
            return copy.deepcopy(self.campaign)
        if method == "POST" and path == "/api/campaigns/6/test":
            return True
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(server, "_request", f)
    monkeypatch.setenv("LISTMONK_TEST_RECIPIENTS", ALLOWED)
    return f


def _sent(fake):
    return [c for c in fake.calls if c[0] == "POST"]


def test_sends_saved_campaign_to_all_test_addresses_by_default(fake):
    out = server.send_test(campaign_id=6)
    (_, _, token, body), = _sent(fake)
    assert token is None                                  # read credential, not send
    assert body["subscribers"] == ["tester.one@example.com", "tester.two@example.org"]
    assert body["body"] == "<p>Saved body</p>" and body["subject"] == "Hello"
    assert body["template_id"] == 5 and body["lists"] == [7]
    assert out["sent_test_to"] == body["subscribers"]


def test_can_pick_a_subset_case_insensitively(fake):
    server.send_test(campaign_id=6, recipients=["TESTER.ONE@example.com"])
    assert _sent(fake)[0][3]["subscribers"] == ["tester.one@example.com"]


@pytest.mark.parametrize("bad", ["someone@else.com", "tester.one@example.com.evil.io", ""])
def test_any_address_outside_allowlist_refuses_the_whole_call(fake, bad):
    with pytest.raises(ToolError, match="test address"):
        server.send_test(campaign_id=6, recipients=["tester.one@example.com", bad])
    assert fake.calls == []


def test_refuses_when_no_allowlist_configured(fake, monkeypatch, tmp_path):
    monkeypatch.delenv("LISTMONK_TEST_RECIPIENTS")
    monkeypatch.setattr(server, "TEST_RECIPIENTS_FILE", tmp_path / "none")
    with pytest.raises(ToolError, match="LISTMONK_TEST_RECIPIENTS"):
        server.send_test(campaign_id=6)
    assert fake.calls == []


def test_allowlist_can_come_from_file(fake, monkeypatch, tmp_path):
    monkeypatch.delenv("LISTMONK_TEST_RECIPIENTS")
    f = tmp_path / "test-recipients"
    f.write_text("only.me@example.com\n")
    monkeypatch.setattr(server, "TEST_RECIPIENTS_FILE", f)
    server.send_test(campaign_id=6)
    assert _sent(fake)[0][3]["subscribers"] == ["only.me@example.com"]


def test_content_cannot_be_supplied_by_caller():
    """No subject/body parameters: it can only send what's saved."""
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "send_test")
    params = set(tool.input_schema["properties"])
    assert params == {"campaign_id", "recipients"}
