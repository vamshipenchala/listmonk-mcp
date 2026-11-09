"""add_matching_to_list and blocklist_emails: bounded, targeted, reported."""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from listmonk_mcp import server

LISTS = {
    8: {"id": 8, "name": "Win-Back Reminder Nov", "tags": []},
    9: {"id": 9, "name": "Android Reminder 1", "tags": []},
    10: {"id": 10, "name": "Holiday promo", "tags": ["cmo"]},
    4: {"id": 4, "name": "Destiny Mobile Apps Waitlist ", "tags": ["iOS", "Android"]},
}


class Fake:
    def __init__(self, match_count=12, subscribers=None):
        self.match_count = match_count
        self.subscribers = subscribers or {}
        self.calls = []

    def __call__(self, method, path, token=None, **kw):
        self.calls.append((method, path, token, kw.get("params"), kw.get("json")))
        if method == "GET" and path.startswith("/api/lists/"):
            return LISTS[int(path.rsplit("/", 1)[1])]
        if method == "GET" and path == "/api/subscribers":
            q = kw["params"].get("query", "")
            if "IN (" in q:
                found = [s for e, s in self.subscribers.items() if f"'{e}'" in q]
                return {"results": found, "total": len(found)}
            return {"results": [], "total": self.match_count}
        if method == "PUT":
            return True
        raise AssertionError(f"unexpected {method} {path}")

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(server, "_request", f)
    return f


Q = "subscribers.attribs->>'plan' = 'plus'"

# --- add_matching_to_list ----------------------------------------------------


def test_adds_matching_subscribers_and_reports(fake):
    out = server.add_matching_to_list(query=Q, list_id=8)
    assert out["added_up_to"] == 12 and out["list"] == "Win-Back Reminder Nov"
    (m, path, token, _, body), = fake.writes()
    assert (m, path, token) == ("PUT", "/api/subscribers/query/lists", None)
    assert body == {"query": Q, "action": "add", "target_list_ids": [8], "status": "confirmed"}


def test_counts_with_the_same_query_before_writing(fake):
    server.add_matching_to_list(query=Q, list_id=8)
    count_call = next(c for c in fake.calls if c[1] == "/api/subscribers")
    assert count_call[3] == {"query": Q, "per_page": 1}
    assert fake.calls.index(count_call) < fake.calls.index(fake.writes()[0])


@pytest.mark.parametrize("list_id", [9, 10])
def test_reminder_prefix_or_cmo_tag_lists_are_allowed(fake, list_id):
    server.add_matching_to_list(query=Q, list_id=list_id)
    assert len(fake.writes()) == 1


def test_main_lists_are_refused(fake):
    with pytest.raises(ToolError, match="Win-Back Reminder"):
        server.add_matching_to_list(query=Q, list_id=4)
    assert fake.writes() == []


def test_over_cap_is_refused(fake):
    fake.match_count = 501
    with pytest.raises(ToolError, match="501"):
        server.add_matching_to_list(query=Q, list_id=8)
    assert fake.writes() == []


def test_zero_matches_writes_nothing(fake):
    fake.match_count = 0
    out = server.add_matching_to_list(query=Q, list_id=8)
    assert out["added_up_to"] == 0 and fake.writes() == []


@pytest.mark.parametrize("q", ["", "   ", "\n"])
def test_empty_query_is_refused_because_it_means_everyone(fake, q):
    with pytest.raises(ToolError, match="every subscriber"):
        server.add_matching_to_list(query=q, list_id=8)
    assert fake.calls == []


# --- blocklist_emails --------------------------------------------------------


def sub(i, email, lists=("Win-Back",), status="enabled"):
    return {"id": i, "email": email, "status": status,
            "lists": [{"id": n, "name": name} for n, name in enumerate(lists, 1)]}


def test_blocklists_found_ids_reports_missing_and_records_lists(fake):
    fake.subscribers = {"a@x.com": sub(1, "a@x.com", ("Waitlist", "Win-Back")),
                        "b@y.org": sub(2, "b@y.org")}
    out = server.blocklist_emails(["A@x.com", "b@y.org", "nobody@z.io"])
    (m, path, token, _, body), = fake.writes()
    assert (m, path, token, body) == ("PUT", "/api/subscribers/blocklist", None, {"ids": [1, 2]})
    assert out["not_found"] == ["nobody@z.io"]
    assert out["blocklisted"] == {"a@x.com": ["Waitlist", "Win-Back"], "b@y.org": ["Win-Back"]}
    assert "unsubscribed from every list" in out["note"]


def test_already_blocklisted_are_reported_not_rewritten(fake):
    fake.subscribers = {"a@x.com": sub(1, "a@x.com", status="blocklisted")}
    out = server.blocklist_emails(["a@x.com"])
    assert out["already_blocklisted"] == ["a@x.com"] and fake.writes() == []


def test_nothing_found_writes_nothing(fake):
    out = server.blocklist_emails(["ghost@x.com"])
    assert out["not_found"] == ["ghost@x.com"] and fake.writes() == []


def test_cap_of_50(fake):
    with pytest.raises(ToolError, match="50"):
        server.blocklist_emails([f"u{i}@x.com" for i in range(51)])
    assert fake.calls == []


@pytest.mark.parametrize("bad", [
    "subscribers.id > 0",                 # SQL, not an address
    "a@x.com' OR '1'='1",                 # injection attempt
    "a@x.com,b@x.com",                    # two in one
    "not-an-email",
    "%@gmail.com",                        # wildcard
])
def test_only_exact_addresses_are_accepted(fake, bad):
    with pytest.raises(ToolError, match="exact email"):
        server.blocklist_emails(["ok@x.com", bad])
    assert fake.calls == []


def test_tools_are_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"add_matching_to_list", "blocklist_emails"} <= names
