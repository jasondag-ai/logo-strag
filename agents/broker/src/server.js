import express from 'express';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { z } from 'zod';

import { authMiddleware } from './auth.js';
import {
  submitForReview,
  getPendingReview,
  submitReview,
  getReviewStatus,
} from './tools.js';

// MCP server is built fresh per request so each handler closes over the
// authenticated caller. Stateless HTTP, no session reuse.
function buildMcpServer(caller) {
  const server = new McpServer({
    name: 'stragentic-broker',
    version: '1.0.0',
  });

  server.registerTool(
    'submit_for_review',
    {
      description: 'Queue work for Manus to review. Returns the row id.',
      inputSchema: {
        content: z.any().describe('The work to review. Code, doc, plan, whatever.'),
        context: z.string().optional().describe('What to look for, constitution refs, etc.'),
        review_type: z.enum(['code', 'doc', 'strategy', 'deal']),
      },
    },
    async ({ content, context, review_type }) => {
      const row = await submitForReview({ caller, content, context, review_type });
      return { content: [{ type: 'text', text: JSON.stringify(row) }] };
    }
  );

  server.registerTool(
    'get_pending_review',
    {
      description:
        'Claim the oldest pending row. Flips status to in_review atomically. Returns null when empty.',
      inputSchema: {},
    },
    async () => {
      const row = await getPendingReview();
      return { content: [{ type: 'text', text: JSON.stringify(row) }] };
    }
  );

  server.registerTool(
    'submit_review',
    {
      description:
        'Attach findings to a review. status=complete signs off, status=pending bounces it back.',
      inputSchema: {
        id: z.string().uuid(),
        findings: z.any(),
        status: z.enum(['complete', 'pending']),
      },
    },
    async ({ id, findings, status }) => {
      const result = await submitReview({ id, findings, status });
      return { content: [{ type: 'text', text: JSON.stringify(result) }] };
    }
  );

  server.registerTool(
    'get_review_status',
    {
      description: 'Poll a review row by id. Returns status, findings, reviewed_at.',
      inputSchema: {
        id: z.string().uuid(),
      },
    },
    async ({ id }) => {
      const result = await getReviewStatus({ id });
      return { content: [{ type: 'text', text: JSON.stringify(result) }] };
    }
  );

  return server;
}

const app = express();
app.use(express.json({ limit: '5mb' }));

app.get('/health', (_req, res) => {
  res.json({ ok: true, service: 'stragentic-broker' });
});

app.post('/mcp', authMiddleware, async (req, res) => {
  try {
    const server = buildMcpServer(req.caller);
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: undefined,
    });
    res.on('close', () => {
      transport.close();
      server.close();
    });
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
  } catch (err) {
    console.error('[mcp] handler error:', err);
    if (!res.headersSent) {
      res.status(500).json({ error: err.message });
    }
  }
});

// MCP spec also defines GET and DELETE on the endpoint for session-aware
// transports. Stateless mode rejects them with 405 per the spec.
app.get('/mcp', (_req, res) => res.status(405).json({ error: 'method not allowed' }));
app.delete('/mcp', (_req, res) => res.status(405).json({ error: 'method not allowed' }));

const port = process.env.PORT || 3000;
app.listen(port, () => {
  console.log(`stragentic-broker listening on :${port}`);
});
