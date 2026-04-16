# PathGrant: Session Handoff

Generated: 2026-04-16 from commit `0ce7e73` on branch `claude/pathgrant-harvester-3c2Fx`

Cross-checked against `git log --oneline` and actual file/test counts.

---

## Repo state

- Branch: `claude/pathgrant-harvester-3c2Fx`
- Latest commit: `0ce7e73` run_pathgrant.py: skip API key check on --skip-intelligence path
- Working tree: clean, all changes pushed to origin

### Commit log (most recent first)

```
0ce7e73 run_pathgrant.py: skip API key check on --skip-intelligence path
d668322 gitignore pathgrant/reports/ run artifacts
4bdc606 feat: run_pathgrant.py orchestrator -- single-command pipeline, dry-run verified
4a703f8 verify_grant.py: --user-agent override and --update in-place editor
2e017ce Add pathgrant/tools/verify_grant.py: standalone CLI verification tool
0944436 reporter.py: stale data warning system for Alerts section
3d7ee67 Fix saskatchewan_lotteries_community_grant_program deadline metadata
9a764a5 reporter.py: add Complete Grant Register section
c660834 reporter.py: add Methodology section between Advisory Notes and Sources
2517cf5 Em dash post-process strip + fresh deadline calc in Alerts
c76d9c5 intelligence.py: CTA deadline injection, style rules, concrete ROI math
64c9131 reporter.py: integrate intelligence module via --intelligence-path
3fc57dc gitignore pathgrant/intelligence/ run artifacts
7cacd9d intelligence.py chunk 4/4: orchestrator, CLI, dry-run verification
8efb530 intelligence.py chunk 3/4: parser, API client, retry/backoff, section wrappers
f4f2eb5 intelligence.py chunk 2/4: 7 prompt builders
3ab8835 intelligence.py chunk 1/4: constants, schemas, validator, sentinels
8a04f3c Add reporter.py - template v1
```

---

## File inventory

| File | Lines | Tests | Role |
|---|---|---|---|
| `run_pathgrant.py` (repo root) | 783 | 16/16 | Pipeline orchestrator. Single-command runner. |
| `pathgrant/engine/intelligence.py` | 1733 | 81/81 | LLM narrative generation (9 Claude API calls per run) |
| `pathgrant/engine/reporter.py` | 1743 | 7/7 | Markdown report template with 14-section layout |
| `pathgrant/engine/scorer.py` | 926 | (inline) | Deterministic grant scoring: signals, penalties, eliminators |
| `pathgrant/engine/matcher.py` | 228 | -- | Scores grants against a client, returns ranked list |
| `pathgrant/tools/verify_grant.py` | 588 | -- | Grant data verification tool with live fetch and --update |
| `pathgrant/data/grants_verified.json` | -- | -- | 20 verified grants |
| `pathgrant/data/grants_unverified.json` | -- | -- | 8 unverified grants (Research Queue) |
| `pathgrant/clients/emerge_academy_profile.json` | -- | -- | First client profile |
| `pathgrant/clients/sacral_solutions_profile.json` | -- | -- | Second client profile |

---

## Actual layout (overrides any older handoff assumptions)

- Client profiles: `pathgrant/clients/<id>_profile.json`
- Reporter CLI: `--client <path>` (takes full path, not client_id) and `--reports-root <dir>`
- Intelligence CLI: `--client <client_id>` (takes string id, not path)
- Sentinel markers: `<generation failed: ...>` / `[generation failed: ...]` and `metadata.api_failures`
- Cost: computed from `metadata.api_call_log` token counts at claude-sonnet-4-6 pricing ($3/$15 per M)
- Top grant: from `metadata.top_grants_covered` list
- Reports output: `pathgrant/reports/<client_id>/<ts>.md` + `latest.md` (gitignored)
- Intelligence output: `pathgrant/intelligence/<client_id>/<ts>.json` + `latest.json` (gitignored)

---

## Run commands

Full pipeline (requires ANTHROPIC_API_KEY):

```bash
python3 run_pathgrant.py --client emerge_academy
```

Reporter only (skip intelligence, no API key needed):

```bash
python3 run_pathgrant.py --client emerge_academy --skip-intelligence
```

Dry run (print prompts, no API calls, no files written):

```bash
python3 run_pathgrant.py --client emerge_academy --dry-run
```

Grant verification tool:

```bash
python3 pathgrant/tools/verify_grant.py --list
python3 pathgrant/tools/verify_grant.py --grant-id <id>
python3 pathgrant/tools/verify_grant.py --grant-id <id> --update
```

Inline tests:

```bash
python3 run_pathgrant.py --test              # 16 tests
python3 pathgrant/engine/intelligence.py --test  # 81 tests
python3 pathgrant/engine/reporter.py --test      # 7 tests
```

---

## Last live run summary (2026-04-15)

```
------------------------------------------------------------
  PathGrant Run Summary
------------------------------------------------------------
  Client:          The Emerge Academy (emerge_academy)
  Top-N:           3
  Grants covered:  3
  Top grant:       saskatchewan_first_nations_and_metis_community_partnership
  Report:          pathgrant/reports/emerge_academy/latest.md
  Intelligence:    pathgrant/intelligence/emerge_academy/latest.json
  API calls:       9
  Cost:            $0.2641
  Runtime:         286.4s (intel 286.3s + reporter 0.1s)
------------------------------------------------------------
```

All 9 API calls succeeded. Zero failures, zero sentinels, zero parse retries.

---

## Report section order (14 sections, with intelligence)

1. Alerts: time-sensitive deadlines (+ stale data warning when applicable)
2. Client Snapshot
3. Top Matches: Program Grants
4. Sponsorships
5. Research Grants
6. SR&ED Assessment
7. Eligibility Risks
8. Research Queue (unverified)
9. Advisory Notes
10. Complete Grant Register
11. 90-Day Action Plan
12. Engagement Options
13. Methodology
14. Sources

---

## Directive v2.0 style rules (enforced in intelligence.py system prompt)

- No em dashes (deterministic post-process strip in intelligence.py + reporter.py)
- No "not X but Y" rhetorical construction
- No throat-clearing ("it is worth noting", "it is important to", "this is critical")
- No buzzwords: robust, nuanced, holistic, leverage (as verb), ecosystem
- Plain declarative sentences, operator tone

---

## Data fixes applied this session

**Saskatchewan Lotteries Community Grant Program** (commit `3d7ee67`):
- `intake_close_date`: `"2026-04-15"` removed (set to null); stale April 15 date did not reflect the program's actual published deadlines
- `deadline_notes`: added with Feb 28 southern / Apr 1 + Oct 1 northern text
- `time_sensitive`: removed (southern cycle closed)
- `time_sensitive_note`: removed (contained stale data)
- `last_verified`: updated to `2026-04-15`
- Score impact: 90 dropped to 75 (lost +15 time_sensitive signal). Still Tier 1.

---

## Do not touch

- `pathgrant/engine/intelligence.py`: 1733 lines, 81/81 tests. Stable.
- `pathgrant/engine/reporter.py`: 1743 lines, 7/7 tests. Stable.
- `pathgrant/tools/verify_grant.py`: 588 lines. Stable.

Any changes to these files require rerunning their test suites before committing.

---

## First client status

- Client: Emerge Academy (Cheryl Haas)
- Contract: $4K/month, $1K paid, $3K deferred pending capital raise
- Top grant: SK First Nations and Metis Community Partnership Projects, score 92, deadline May 31 2026 (46 days from April 15)
- Saskatchewan Lotteries: 2026 southern cycle closed (Feb 28 passed). Target Feb 2027.
- SK First Nations and Metis Sponsorships: score 80, rolling intake, pool opened April 2 2026
- Incorporation: in_progress. Hard prerequisite for all three applications.
- Next action: deliver Gamma PDF, initiate SK Partnership Projects funder contact

---

## Next steps in order

1. **Deliver Gamma PDF to Cheryl Haas**: report is at `pathgrant/reports/emerge_academy/latest.md`, ready for Gamma conversion
2. **Standardize client profile intake format** (prereq for Telegram bot / second client onboarding): define a canonical schema that the pipeline validates against, so new clients can be onboarded without hand-editing JSON
3. **Sacral Solutions report**: second client profile exists at `pathgrant/clients/sacral_solutions_profile.json`; run `python3 run_pathgrant.py --client sacral_solutions` when ready
4. **Prompt caching investigation** (deferred, non-blocking): all live runs show `cache_creation_input_tokens: 0` / `cache_read_input_tokens: 0`; system prompt may be below Anthropic's ~1024-token caching minimum

---

## Communication style

Straight shooter. Bullet points. No flattery. No em dashes. No fluff. Challenge when data contradicts claims. Write like a $500/hr consultant.
