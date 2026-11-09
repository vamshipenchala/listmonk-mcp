# listmonk-mcp

A local MCP server that lets Claude work with listmonk at
`send.destinyaiastrology.com`.

## Why this exists

listmonk speaks REST. Claude connectors speak MCP. Pointing a Claude connector
straight at listmonk's API returns:

```
HTTP 403  {"message":"invalid session"}
```

— because nothing sends the `Authorization: token user:token` header listmonk
expects. This server sits in between: Claude speaks MCP to it, it speaks REST
to listmonk and attaches the token.

## Setup

The token lives in a file, never in the code, never in git, never in chat:

```bash
mkdir -p ~/.listmonk
printf 'claude_cmo_read:YOUR_TOKEN' > ~/.listmonk/token
chmod 600 ~/.listmonk/token
```

It must be `username:token`. A bare token gets `invalid API credentials`.

Already registered with Claude Code (user scope). To re-add:

```bash
claude mcp add listmonk --scope user -- \
  ~/.local/bin/uv --directory ~/listmonk-mcp run python -m listmonk_mcp.server
```

## Tools

**Read:** `list_lists`, `get_list`, `list_subscribers`, `list_campaigns`,
`get_campaign`, `get_campaign_analytics`, `list_templates`

**Write:** `create_campaign` (always a draft), `update_campaign`,
`schedule_campaign`, `set_campaign_status`, `create_list`, `add_subscriber`,
`update_subscriber`

## What it will not do

**It cannot send a campaign.** In listmonk, scheduling and sending are the same
endpoint — `PUT /api/campaigns/{id}/status` with `scheduled` vs `running` — so
the boundary is enforced on the value, not the route. `running` is refused
before any network call, whatever asks for it.

Also absent: deleting campaigns, lists or subscribers; bulk import; arbitrary
SQL.

The reason is asymmetry. Everything this server can do is editable or
reversible. A send to ~200K recipients is not. It also reads text other people
wrote — subscriber names, campaign bodies, bounce payloads — so every response
is labelled untrusted data, not instructions.

To add sending later: drop `"running"` from `SEND_IS_NOT_EXPOSED`, add it to
`ALLOWED_STATUS_CHANGES`, and point the token at `claude_cmo_send`. Two tests
will fail, by design — they exist to make that a deliberate act.

## Tests

```bash
uv run pytest tests/ -q
```

No network required.
