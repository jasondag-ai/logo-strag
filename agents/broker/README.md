# stragentic-broker

MCP HTTP server. One job: be the inbox between Claude Code and Manus so the
Build-and-Verify loop (Constitution section 8) runs without Jay relaying
files by hand.

No business logic, no review intelligence. Manus is the brain. This is the
queue.

## Tools exposed

| Tool | Caller | Purpose |
|---|---|---|
| `submit_for_review(content, context, review_type)` | Claude | Insert a pending row. Returns `{ id, status }`. |
| `get_pending_review()` | Manus | Atomically claim the oldest pending row. Flips to `in_review`. Returns the row or null. |
| `submit_review(id, findings, status)` | Manus | Attach findings. `status` is `complete` or `pending` (bounce back). |
| `get_review_status(id)` | Claude | Poll. Returns `{ status, findings, reviewed_at }`. |

Auth is a single header: `X-Stragentic-Auth: <token>`. Two valid tokens,
one per caller. The server logs which caller authenticated each request,
never the token itself.

## Files

```
agents/broker/
  src/
    server.js     Express + MCP transport + route wiring
    tools.js      The four tool implementations
    db.js         Supabase client
    auth.js       Token check middleware
  migrations/
    001_review_queue.sql
  test/
    full_loop.js  End-to-end smoke test
  package.json
  railway.toml
  .env.example
```

## 1. Supabase setup

If the existing Stragentic Supabase project is alive, reuse it. Otherwise
create a fresh one (free tier is fine for the broker).

1. Go to https://supabase.com/dashboard, create project `stragentic-broker`.
2. SQL Editor, paste `migrations/001_review_queue.sql`, run.
3. Project Settings, API, copy:
   - `Project URL` (becomes `SUPABASE_URL`)
   - `service_role` key under "Project API keys" (becomes
     `SUPABASE_SERVICE_KEY`). This bypasses RLS, do not expose it
     client-side.

Verify the migration ran:

```sql
select count(*) from review_queue;       -- should return 0
select claim_next_review();              -- should return empty set
```

## 2. Generate auth tokens (on the Mac)

```bash
openssl rand -hex 32 | pbcopy        # Claude token, paste into .env
openssl rand -hex 32 | pbcopy        # Manus token, paste into .env
```

Store them in 1Password under `Stragentic / Broker tokens`. They cannot be
recovered later, only rotated.

## 3. Local run

```bash
cd agents/broker
cp .env.example .env
# Fill in SUPABASE_URL, SUPABASE_SERVICE_KEY, CLAUDE_TOKEN, MANUS_TOKEN
npm install
npm run dev
```

Health check:

```bash
curl http://localhost:3000/health
# { "ok": true, "service": "stragentic-broker" }
```

Tool call (raw JSON-RPC, just to prove auth works):

```bash
curl -X POST http://localhost:3000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Stragentic-Auth: $CLAUDE_TOKEN" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {}
  }'
```

A missing or wrong token returns 401.

## 4. Railway deploy

One-time setup on the Mac:

```bash
brew install railway
railway login                         # opens browser
```

From the broker directory:

```bash
cd agents/broker
railway init                          # name it stragentic-broker
railway up                            # builds and deploys
```

Set env vars (Railway dashboard, Variables tab on the service):

```
SUPABASE_URL
SUPABASE_SERVICE_KEY
CLAUDE_TOKEN
MANUS_TOKEN
```

PORT is injected by Railway, do not set it.

Generate a public URL: Settings, Networking, "Generate Domain". Copy the
`https://<subdomain>.up.railway.app` URL. The MCP endpoint is at
`/mcp` on that host.

Confirm:

```bash
curl https://<subdomain>.up.railway.app/health
```

Redeploy after a code change:

```bash
git add agents/broker
git commit -m "broker: <what changed>"
git push                              # if Railway is GitHub-linked
# or
railway up                            # direct deploy
```

## 5. End-to-end smoke test

Once Manus is wired to the broker (next section), from the Mac:

```bash
cd agents/broker
# .env should already have CLAUDE_TOKEN. Add BROKER_URL.
echo "BROKER_URL=https://<subdomain>.up.railway.app" >> .env
npm run test:loop
```

The script:
1. Submits a fake code review as Claude.
2. Polls every 5s up to 2 minutes.
3. Prints findings when Manus marks it complete, or times out with the
   last seen state.

A 2-minute timeout means Manus is not pulling. Check the Manus MCP
config block and confirm its token matches `MANUS_TOKEN` in Railway.

## 6. Manus MCP configuration

Paste the block below into Manus, MCP settings, "Add custom server".
Substitute the real URL and token.

```json
{
  "mcpServers": {
    "stragentic-broker": {
      "url": "https://<subdomain>.up.railway.app/mcp",
      "transport": "streamable-http",
      "headers": {
        "X-Stragentic-Auth": "<MANUS_TOKEN value>"
      }
    }
  }
}
```

Save. Manus should list four tools: `submit_for_review`,
`get_pending_review`, `submit_review`, `get_review_status`. In practice
Manus only uses `get_pending_review` and `submit_review`. The other two
are present because the same server serves Claude too.

Standing instruction to give Manus once the MCP is connected:

> Every 60 seconds (or on demand), call `get_pending_review` on
> stragentic-broker. If it returns a row, review the content against the
> context and the Stragentic Constitution, then call `submit_review` with
> findings and `status: complete` if it passes, `status: pending` if it
> needs rework. If null, do nothing.

## 7. Claude Code MCP configuration

On the Mac, in `~/.claude/mcp_servers.json` (or via `claude mcp add`):

```json
{
  "mcpServers": {
    "stragentic-broker": {
      "url": "https://<subdomain>.up.railway.app/mcp",
      "transport": "streamable-http",
      "headers": {
        "X-Stragentic-Auth": "<CLAUDE_TOKEN value>"
      }
    }
  }
}
```

Restart Claude Code. The four broker tools should show in `/mcp`.

## 8. Constitution checks baked in

- No em or en dashes in code, comments, or docs.
- No banned AI-isms (section 5 of the Constitution).
- One job per agent. This broker does the queue, nothing else.
- Findings are unopinionated JSONB. The reviewer decides shape, the
  broker just stores and returns it.
- Service-role key only lives in Railway env vars, never in git, never
  in logs.

## 9. Known limits

- Stateless transport. No SSE push from server to client. Claude polls
  via `get_review_status`. Good enough at queue depths under a few
  hundred per day.
- No retention policy. Old `complete` rows accumulate. Add a cron later
  if it matters.
- No multi-tenant scoping. Two tokens, two roles, that is the security
  model.

## 10. Self-review handoff for v1

Per the Constitution, this build itself needs Manus QA before going
live. The loop does not exist yet, so Jay relays the build manually:

1. Push this branch (`claude/elegant-thompson-6XEnK`) to GitHub.
2. Paste the diff into Manus with the prompt: "Review the
   stragentic-broker build against the Constitution. Flag anything that
   breaks section 5, anything that adds scope beyond a queue, anything
   that leaks the service-role key, anything that breaks atomicity in
   `get_pending_review`. Return findings as a JSON object."
3. Apply fixes if any. Redeploy.
4. Run the smoke test from section 5 above.
5. Once it goes green, the broker is live and subsequent builds use the
   loop natively.
