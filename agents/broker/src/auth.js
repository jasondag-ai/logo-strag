// Token header check. Two tokens, one role each.
// Returns 401 on missing or invalid. Logs the caller name, never the token.
export function authMiddleware(req, res, next) {
  const token = req.header('X-Stragentic-Auth');
  if (!token) {
    return res.status(401).json({ error: 'missing X-Stragentic-Auth header' });
  }

  const claudeToken = process.env.CLAUDE_TOKEN;
  const manusToken = process.env.MANUS_TOKEN;
  if (!claudeToken || !manusToken) {
    console.error('[auth] CLAUDE_TOKEN or MANUS_TOKEN not set');
    return res.status(500).json({ error: 'server misconfigured' });
  }

  if (token === claudeToken) {
    req.caller = 'claude';
  } else if (token === manusToken) {
    req.caller = 'manus';
  } else {
    return res.status(401).json({ error: 'invalid token' });
  }

  console.log(`[auth] ${req.method} ${req.path} caller=${req.caller}`);
  next();
}
