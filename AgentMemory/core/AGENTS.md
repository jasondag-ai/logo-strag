# Agent Configurations — Master Memory File

## Agent Architecture Overview

- **Framework:** [PLACEHOLDER — e.g., OpenClaw, custom, LangChain, CrewAI, etc.]
- **Orchestration:** [PLACEHOLDER — how agents coordinate]
- **Memory System:** AgentMemory (this system — ~/AgentMemory/)
- **Communication Layer:** Telegram (primary), [PLACEHOLDER — other channels]
- **Config Location (Mac):** ~/.openclaw/

---

## Core Agents

### 1. [PLACEHOLDER — Primary/Orchestrator Agent Name]
- **Role:** [PLACEHOLDER — e.g., task routing, orchestration, user interface]
- **Model:** [PLACEHOLDER — e.g., Claude Opus, GPT-4, etc.]
- **Capabilities:** [PLACEHOLDER]
- **Triggers:** [PLACEHOLDER — how is this agent activated?]
- **Config File:** [PLACEHOLDER — path in ~/.openclaw/]

### 2. [PLACEHOLDER — Research/Analysis Agent Name]
- **Role:** [PLACEHOLDER]
- **Model:** [PLACEHOLDER]
- **Capabilities:** [PLACEHOLDER]
- **Data Sources:** [PLACEHOLDER]
- **Config File:** [PLACEHOLDER]

### 3. [PLACEHOLDER — Business/Operations Agent Name]
- **Role:** [PLACEHOLDER]
- **Model:** [PLACEHOLDER]
- **Capabilities:** [PLACEHOLDER]
- **Business Focus:** Spinaline, Rustica Strategies, Stragentic
- **Config File:** [PLACEHOLDER]

### 4. [PLACEHOLDER — Communication Agent Name]
- **Role:** [PLACEHOLDER — e.g., Telegram bot, email management]
- **Model:** [PLACEHOLDER]
- **Capabilities:** [PLACEHOLDER]
- **Integrations:** Telegram, [PLACEHOLDER — email, Discord, etc.]
- **Config File:** [PLACEHOLDER]

---

## Agent Capabilities Matrix

| Agent | Research | Code | Business | Comms | Memory |
|-------|----------|------|----------|-------|--------|
| [Agent 1] | [PLACEHOLDER] | [PLACEHOLDER] | [PLACEHOLDER] | [PLACEHOLDER] | [PLACEHOLDER] |
| [Agent 2] | [PLACEHOLDER] | [PLACEHOLDER] | [PLACEHOLDER] | [PLACEHOLDER] | [PLACEHOLDER] |
| [Agent 3] | [PLACEHOLDER] | [PLACEHOLDER] | [PLACEHOLDER] | [PLACEHOLDER] | [PLACEHOLDER] |
| [Agent 4] | [PLACEHOLDER] | [PLACEHOLDER] | [PLACEHOLDER] | [PLACEHOLDER] | [PLACEHOLDER] |

---

## MCP Server Integrations

Detected from `.claude.json`:
- **Discord Plugin** — [PLACEHOLDER — what agents use this? purpose?]
- **Telegram Plugin** — Primary user communication channel
- **iMessage Plugin** — [PLACEHOLDER — use case]
- **FakeChat Plugin** — Testing/development conversations

## Agent Communication Protocol

- **User → Agent:** Telegram messages, Claude Code CLI
- **Agent → User:** Telegram responses, CLI output
- **Agent → Agent:** [PLACEHOLDER — how do agents communicate with each other?]
- **Memory Sync:** ~/AgentMemory/scripts/memory-sync.sh

---

## Configuration Files (Mac — ~/.openclaw/)

- `SOUL.md` — [PLACEHOLDER — agent personality/values definition]
- `USER.md` — [PLACEHOLDER — user context for agents]
- `AGENTS.md` — [PLACEHOLDER — agent roster and configs]
- [PLACEHOLDER — additional config files]

---

## Deployment

- **Hosting:** [PLACEHOLDER — local, cloud, hybrid?]
- **Always-On Agents:** [PLACEHOLDER — which agents run continuously?]
- **On-Demand Agents:** [PLACEHOLDER — which agents are triggered as needed?]
- **Monitoring:** [PLACEHOLDER — how do you monitor agent health?]

---

*Last updated: 2026-04-08*
