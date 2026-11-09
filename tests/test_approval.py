"""Send/schedule approval: Claude can request, only a human click can execute.

A fake listmonk records every call, so these tests prove which credential was
used and that nothing is written before the human approves.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from listmonk_mcp import server

HOST = "listmonkmcp.destinyaiastrology.com"

CAMPAIGN = {
    "id": 7, "name": "Diwali follow-up", "subject": "A note <b>from</b> Destiny",
    "body": "<p>Hello</p>", "altbody": None, "content_type": "html",
    "from_email": "Destiny <support@destinyaiastrology.com>", "template_id": 6,
    "tags": ["iOS"], "headers": [], "attribs": {}, "messenger": "email",
    "type": "regular", "status": "draft", "send_at": None,
    "updated_at": "2026-11-08T03:00:00Z",
    "lists": [{"id": 5, "name": "Android India"}, {"id": 3, "name": "Win-Back"}],
}
LIVE_COUNTS = {5: 338, 3: 44}
# listmonk caches list counts; a just-filled list reports 0. Never use this.
LISTS = {5: {"id": 5, "subscriber_count": 0}, 3: {"id": 3, "subscriber_count": 0}}


class FakeListmonk:
    def __init__(self):
        self.campaign = copy.deepcopy(CAMPAIGN)
        self.calls: list[tuple[str, str, str | None, object]] = []

    def __call__(self, method, path, token=None, **kw):
        self.calls.append((method, path, token, kw.get("json")))
        if method == "GET" and path == "/api/campaigns/7":
            return copy.deepcopy(self.campaign)
        if method == "GET" and path.startswith("/api/lists/"):
            return LISTS[int(path.rsplit("/", 1)[1])]
        if method == "GET" and path == "/api/subscribers":
            params = kw["params"]
            assert params["per_page"] == 1
            return {"results": [], "total": LIVE_COUNTS[params["list_id"]]}
        if method == "PUT":
            return {"ok": True}
        raise AssertionError(f"unexpected call {method} {path}")

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture
def lm(monkeypatch):
    fake = FakeListmonk()
    monkeypatch.setattr(server, "_request", fake)
    monkeypatch.setattr(server, "_send_token", lambda: "claude_cmo_send:SENDTOKEN")
    monkeypatch.setenv("MCP_PUBLIC_HOST", HOST)
    server._approvals.clear()
    return fake


@pytest.fixture
def client():
    return TestClient(Starlette(routes=server.APPROVAL_ROUTES), base_url=f"https://{HOST}")


def _token_from(result) -> str:
    url = result["approval_url"]
    assert url.startswith(f"https://{HOST}/approve/")
    return url.rsplit("/", 1)[1]


def _future(hours=24) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Requesting ---------------------------------------------------------------


def test_request_send_writes_nothing(lm):
    result = server.request_send(campaign_id=7)
    assert _token_from(result)
    assert lm.writes() == [], "requesting must not change anything in listmonk"
    assert "Nothing has been sent" in result["status"]


def test_tools_never_use_the_send_credential(lm):
    server.request_send(campaign_id=7)
    assert all(token is None for _, _, token, _ in lm.calls)


def test_request_send_needs_remote_deployment(lm, monkeypatch):
    monkeypatch.delenv("MCP_PUBLIC_HOST")
    with pytest.raises(ToolError, match="remote"):
        server.request_send(campaign_id=7)


def test_request_send_refuses_finished_campaign(lm):
    lm.campaign["status"] = "finished"
    with pytest.raises(ToolError, match="finished"):
        server.request_send(campaign_id=7)


def test_request_schedule_refuses_past_time(lm):
    with pytest.raises(ToolError, match="future"):
        server.request_send(campaign_id=7, send_at="2020-01-01T00:00:00Z")


def test_request_schedule_refuses_naive_time(lm):
    with pytest.raises(ToolError, match="timezone"):
        server.request_send(campaign_id=7, send_at="2030-01-01T09:00:00")


def test_set_campaign_status_can_no_longer_schedule(lm):
    with pytest.raises(ToolError, match="request_send"):
        server.set_campaign_status(campaign_id=7, status="scheduled")


# --- Approving ----------------------------------------------------------------


def test_approval_page_shows_what_will_happen_and_writes_nothing(lm, client):
    token = _token_from(server.request_send(campaign_id=7))
    r = client.get(f"/approve/{token}")
    assert r.status_code == 200
    assert "382" in r.text                       # 338 + 44 recipients, upper bound
    assert "A note &lt;b&gt;from&lt;/b&gt; Destiny" in r.text   # escaped
    assert "Send now" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert lm.writes() == []


def test_approve_send_now_uses_send_credential_once(lm, client):
    token = _token_from(server.request_send(campaign_id=7))
    r = client.post(f"/approve/{token}", data={"confirm": "yes"})
    assert r.status_code == 200
    assert lm.writes() == [
        ("PUT", "/api/campaigns/7/status", "claude_cmo_send:SENDTOKEN", {"status": "running"})
    ]


def test_link_is_single_use(lm, client):
    token = _token_from(server.request_send(campaign_id=7))
    client.post(f"/approve/{token}", data={"confirm": "yes"})
    r = client.post(f"/approve/{token}", data={"confirm": "yes"})
    assert r.status_code == 410
    assert len(lm.writes()) == 1


def test_post_without_confirmation_does_nothing(lm, client):
    token = _token_from(server.request_send(campaign_id=7))
    r = client.post(f"/approve/{token}")
    assert r.status_code == 400
    assert lm.writes() == []


def test_unknown_token_is_rejected(lm, client):
    r = client.post("/approve/not-a-real-token", data={"confirm": "yes"})
    assert r.status_code == 410
    assert lm.writes() == []


def test_expired_link_does_nothing(lm, client):
    token = _token_from(server.request_send(campaign_id=7))
    server._approvals[token].expires_at = 0
    r = client.post(f"/approve/{token}", data={"confirm": "yes"})
    assert r.status_code == 410
    assert lm.writes() == []


@pytest.mark.parametrize("field,value", [
    ("body", "<p>Click here to win</p>"),
    ("subject", "Changed"),
    ("lists", [{"id": 4, "name": "Everyone"}]),
    ("updated_at", "2026-11-08T04:00:00Z"),
])
def test_edit_after_request_invalidates_approval(lm, client, field, value):
    """Approval covers exactly what the human reviewed."""
    token = _token_from(server.request_send(campaign_id=7))
    lm.campaign[field] = value
    r = client.post(f"/approve/{token}", data={"confirm": "yes"})
    assert r.status_code == 409
    assert lm.writes() == []


def test_approve_schedule_sends_full_campaign_then_schedules(lm, client):
    when = _future()
    token = _token_from(server.request_send(campaign_id=7, send_at=when))
    assert "Schedule" in client.get(f"/approve/{token}").text
    r = client.post(f"/approve/{token}", data={"confirm": "yes"})
    assert r.status_code == 200
    (m1, p1, t1, body), (m2, p2, t2, status) = lm.writes()
    assert (m1, p1, t1) == ("PUT", "/api/campaigns/7", "claude_cmo_send:SENDTOKEN")
    assert body["send_at"] == when
    # listmonk's update replaces the campaign: nothing may be dropped
    for k in ("name", "subject", "body", "content_type", "from_email", "template_id", "tags", "type"):
        assert body[k] == CAMPAIGN[k], k
    assert body["lists"] == [5, 3]
    assert (m2, p2, t2, status) == (
        "PUT", "/api/campaigns/7/status", "claude_cmo_send:SENDTOKEN", {"status": "scheduled"})


def test_schedule_time_that_has_passed_is_refused_at_approval(lm, client):
    token = _token_from(server.request_send(campaign_id=7, send_at=_future(hours=1)))
    server._approvals[token].send_at = "2020-01-01T00:00:00Z"
    r = client.post(f"/approve/{token}", data={"confirm": "yes"})
    assert r.status_code == 409
    assert lm.writes() == []


# --- update_campaign ---------------------------------------------------------


def test_update_campaign_keeps_fields_it_was_not_asked_to_change(lm):
    server.update_campaign(campaign_id=7, subject="New subject")
    (_, path, token, body), = lm.writes()
    assert path == "/api/campaigns/7" and token is None
    assert body["subject"] == "New subject"
    for k in ("template_id", "from_email", "tags", "body", "name"):
        assert body[k] == CAMPAIGN[k], k


def test_update_campaign_refuses_scheduled_campaign(lm):
    """Editing after approval would send content nobody approved."""
    lm.campaign["status"] = "scheduled"
    with pytest.raises(ToolError, match="scheduled"):
        server.update_campaign(campaign_id=7, subject="sneaky")
    assert lm.writes() == []


def test_listmonk_error_on_approval_is_shown_not_retried(lm, client, monkeypatch):
    token = _token_from(server.request_send(campaign_id=7))
    real = lm.__call__

    def failing(method, path, token=None, **kw):
        if method == "PUT":
            raise ToolError("listmonk returned 403: permission denied")
        return real(method, path, token, **kw)

    monkeypatch.setattr(server, "_request", failing)
    r = client.post(f"/approve/{token}", data={"confirm": "yes"})
    assert r.status_code == 502
    assert "permission denied" in r.text
    assert client.post(f"/approve/{token}", data={"confirm": "yes"}).status_code == 410


# --- Pause / unschedule (Option A) -------------------------------------------
# listmonk requires campaigns:send for ANY status change, even pausing. So this
# one tool uses the send credential -- but only for values that stop mail.


@pytest.mark.parametrize("status", ["paused", "draft"])
def test_pause_and_unschedule_use_send_credential(lm, status):
    server.set_campaign_status(campaign_id=7, status=status)
    assert lm.writes() == [
        ("PUT", "/api/campaigns/7/status", "claude_cmo_send:SENDTOKEN", {"status": status})
    ]


@pytest.mark.parametrize("status", ["running", "scheduled", "cancelled", "finished", "RUNNING", " running"])
def test_other_statuses_refused_before_send_credential_is_loaded(lm, monkeypatch, status):
    def no_token():
        raise AssertionError("send credential loaded for a refused status")
    monkeypatch.setattr(server, "_send_token", no_token)
    with pytest.raises(ToolError):
        server.set_campaign_status(campaign_id=7, status=status)
    assert lm.calls == []


def test_only_set_campaign_status_ever_uses_send_credential(lm):
    """Every other write tool runs on the read credential."""
    server.request_send(campaign_id=7)
    server.update_campaign(campaign_id=7, subject="x")
    assert all(token is None for _, _, token, _ in lm.calls)
