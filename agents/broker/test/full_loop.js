// End-to-end smoke test.
// Submits a fake code review, polls every 5s up to 2 min, prints findings.
//
// Run after deploy:
//   cd agents/broker
//   npm install
//   node --env-file=.env test/full_loop.js
//
// Required env: BROKER_URL, CLAUDE_TOKEN
// Manus must be configured to pull from the broker, otherwise the row sits
// in pending and the script times out. That timeout is itself a signal.

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const BROKER_URL = process.env.BROKER_URL;
const TOKEN = process.env.CLAUDE_TOKEN;

if (!BROKER_URL || !TOKEN) {
  console.error('set BROKER_URL and CLAUDE_TOKEN before running');
  process.exit(1);
}

function parse(result) {
  const text = result?.content?.[0]?.text;
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function connect() {
  const transport = new StreamableHTTPClientTransport(new URL(`${BROKER_URL}/mcp`), {
    requestInit: {
      headers: { 'X-Stragentic-Auth': TOKEN },
    },
  });
  const client = new Client(
    { name: 'broker-smoke-test', version: '1.0.0' },
    { capabilities: {} }
  );
  await client.connect(transport);
  return client;
}

async function main() {
  const client = await connect();

  console.log('[1/3] submitting fake code review');
  const submission = parse(
    await client.callTool({
      name: 'submit_for_review',
      arguments: {
        content: {
          title: 'smoke test',
          file: 'src/add.js',
          code: 'export function add(a, b) { return a + b; }',
          notes: 'Trivial. Sign it off so we know the loop works.',
        },
        context: 'Broker self-test. No real review needed, just respond.',
        review_type: 'code',
      },
    })
  );
  console.log('   id:', submission.id);
  console.log('   status:', submission.status);

  console.log('[2/3] polling every 5s up to 2 min');
  const deadline = Date.now() + 120000;
  let latest = null;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 5000));
    latest = parse(
      await client.callTool({
        name: 'get_review_status',
        arguments: { id: submission.id },
      })
    );
    const elapsed = Math.round((120000 - (deadline - Date.now())) / 1000);
    console.log(`   t+${elapsed}s status=${latest.status}`);
    if (latest.status === 'complete') break;
  }

  console.log('[3/3] result');
  if (latest?.status === 'complete') {
    console.log(JSON.stringify(latest, null, 2));
    await client.close();
    process.exit(0);
  } else {
    console.log('timed out waiting for review. last seen:');
    console.log(JSON.stringify(latest, null, 2));
    await client.close();
    process.exit(2);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
