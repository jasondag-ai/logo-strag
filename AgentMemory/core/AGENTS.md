# AGENTS — Agent Memory System
# Last Updated: 2026-04-08
# Status: ACTIVE

---

## Agent Architecture Overview

This document defines the multi-agent AI system supporting Jason Dagenais's
businesses and operations. Each agent has defined roles, capabilities, and
communication protocols.

---

## Core Platform

- **Primary AI Engine:** Claude (Anthropic)
  - Model: Claude Opus 4.6 (1M context)
  - Interface: Claude Code (CLI + Web)
- **Communication Hub:** Telegram
- **Memory System:** ~/AgentMemory/ (this repository)
- **Orchestration:** [PLACEHOLDER — how are agents coordinated? OpenClaw? Custom?]

---

## Agent Registry

### 1. Executive Agent (Primary)

- **Name:** [PLACEHOLDER — does this agent have a name?]
- **Role:** Strategic oversight, task delegation, memory management
- **Capabilities:**
  - Cross-business coordination
  - Priority management
  - Agent task assignment
  - Memory system maintenance
- **Communication:** Telegram (direct to Jason)
- **Status:** [PLACEHOLDER — active / in-development]

### 2. Business Operations Agent

- **Name:** [PLACEHOLDER]
- **Role:** Day-to-day business operations support
- **Covers:** All four businesses (Spinaline, Emerge Academy, Rustic Strategies, Stragentic)
- **Capabilities:**
  - Email drafting and management
  - Schedule coordination
  - Document preparation
  - Client communication support
- **Status:** [PLACEHOLDER — active / in-development]

### 3. Research & Analysis Agent

- **Name:** [PLACEHOLDER]
- **Role:** Market research, deal sourcing, competitive analysis
- **Primary Client:** Rustic Strategies (ETA search)
- **Capabilities:**
  - Business valuation research
  - Market analysis
  - Deal flow tracking
  - Industry trend monitoring
- **Status:** [PLACEHOLDER — active / in-development]

### 4. Development Agent

- **Name:** [PLACEHOLDER]
- **Role:** Software development, AI tool building, system maintenance
- **Primary Client:** Stragentic + all businesses
- **Capabilities:**
  - Code generation and review
  - MCP server development
  - Automation scripting
  - System architecture
- **Status:** Active (Claude Code)

### 5. Communications Agent

- **Name:** [PLACEHOLDER]
- **Role:** Social media, content creation, marketing
- **Capabilities:**
  - Content drafting
  - Social media management
  - Brand voice maintenance
  - Newsletter/email campaigns
- **Status:** [PLACEHOLDER — active / in-development]

---

## Agent Communication Protocol

```
Jason <--Telegram--> Executive Agent
                          |
                    +-----+-----+-----+
                    |           |           |           |
               Business    Research    Development  Communications
                Agent       Agent       Agent         Agent
```

### Message Routing
- **Urgent/Critical:** Direct Telegram notification to Jason
- **Daily Summary:** Consolidated update via Telegram
- **Agent-to-Agent:** [PLACEHOLDER — how do agents communicate with each other?]
- **Escalation Path:** Any agent -> Executive Agent -> Jason (Telegram)

---

## OpenClaw Integration

- **Config Location:** `~/.openclaw/`
- **SOUL.md:** Core agent personality and operating principles
- **USER.md:** User profile for agent context
- **AGENTS.md:** Agent role definitions
- **Status:** [PLACEHOLDER — is OpenClaw actively configured? What version?]

---

## Agent Memory Access

All agents read from and write to the shared memory system:

| Agent | Read Access | Write Access |
|-------|------------|--------------|
| Executive | All core/ files | All core/ files |
| Business Ops | USER.md, BUSINESSES.md, COMMUNICATIONS.md | BUSINESSES.md |
| Research | BUSINESSES.md, TOOLS.md | BUSINESSES.md (pipeline section) |
| Development | All core/ files | SYSTEM.md, TOOLS.md |
| Communications | USER.md, BUSINESSES.md, COMMUNICATIONS.md | COMMUNICATIONS.md |

---

## Planned Agents

- [PLACEHOLDER — any additional agents planned?]
- Potential: Financial tracking agent, Health/wellness agent, Calendar agent

---

## Notes

- Agent system is designed to scale as businesses grow
- All agents should respect Jason's time — minimize interruptions, batch updates
- Telegram is the single communication channel to avoid notification fatigue
- Memory system (this repo) is the single source of truth for all agents
