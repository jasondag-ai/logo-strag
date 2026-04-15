"""
pathgrant/engine/intelligence.py

LLM-powered narrative generation for PathGrant client reports.

Takes a client profile + matcher output and makes 9 Claude API calls
(for top_n=3) to produce narrative content for the per-grant and
per-client sections of the report. Output is written to
pathgrant/intelligence/<client_id>/<timestamp>.json (plus latest.json)
for reporter.py to consume alongside the matcher data.

Model:       claude-sonnet-4-6 (configurable)
Temperature: 0.3 (configurable)
Caching:     system prompt + client context cached across all calls.

Call plan for top_n=3:
  1-3  per-grant structured (JSON)        why_qualifies, positioning,
                                          risks, docs, eligibility_risks,
                                          application_strategy
  4    SR&ED assessment (JSON)
  5    90-day action plan (freeform md)   locked ### subheaders
  6    Stragentic CTA (JSON)
  7-9  DIY starter kit (freeform md)      locked ### subheaders
                                          1 per top grant

Dry-run mode prints all prompts and makes zero API calls.

This file is assembled in 4 chunks to avoid stream timeouts:
  chunk 1: constants, schemas, validator, sentinel factories (this file)
  chunk 2: 7 prompt builders
  chunk 3: JSON parser, API client, retry/backoff
  chunk 4: orchestrator, CLI, inline tests
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


_PATHGRANT_ROOT = Path(__file__).resolve().parent.parent
if str(_PATHGRANT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PATHGRANT_ROOT))


# ---------------------------------------------------------------------------
# Defaults and constants
# ---------------------------------------------------------------------------

DEFAULT_VERIFIED_PATH = _PATHGRANT_ROOT / "data" / "grants_verified.json"
DEFAULT_UNVERIFIED_PATH = _PATHGRANT_ROOT / "data" / "grants_unverified.json"
DEFAULT_INTELLIGENCE_ROOT = _PATHGRANT_ROOT / "intelligence"
DEFAULT_CLIENTS_ROOT = _PATHGRANT_ROOT / "clients"

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_TOKENS = 4000
DEFAULT_TOP_N = 3
DIRECTIVE_VERSION = "2.0"

# Minimum score for a grant to be eligible for intelligence generation.
# Matches the reporter's Tier 1 / Tier 2 cutoff; Tier 3 (< 30) is dropped.
MIN_TIER_SCORE = 30

# One retry on JSON parse / schema failure with stricter reprompt; if that
# also fails, fall through to a sentinel.
MAX_PARSE_RETRIES = 1

# Exponential backoff on API-level failures (rate limit, network).
MAX_API_RETRIES = 3


# ---------------------------------------------------------------------------
# System prompt -- shared, cached across all calls
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a senior grant strategy consultant working for Stragentic. Your output goes directly into client-facing reports read by CEOs and CFOs who pay $500/hr for expert advice.

PathGrant Directive v2.0 -- mandatory tone rules:

1. Write for a skeptical CEO/CFO, time-poor. Every sentence earns its place or gets cut.
2. No filler phrases. Never use: "exceptional potential", "exciting opportunity", "comprehensive", "leveraging synergies", "well-positioned", "strong fit". Replace with specific facts.
3. State facts, ranges, and confidence levels. Numbers earn trust.
4. Flag uncertain data explicitly: "(unverified -- confirm with funder)" or "(not publicly disclosed)".
5. Every claim must trace to the supplied grant record, client profile, or widely known public fact. Do not invent amounts, deadlines, or eligibility criteria.
6. Specificity earns trust, not volume. 3 tight sentences beat 7 padded ones.
7. Active voice, present tense, direct address.
8. If a section is inapplicable for this specific client-grant pair, say so in one sentence and explain why. Do not pad.

Output format rules:
- When asked for JSON, return ONLY JSON. No preamble, no code fences, no prose wrapper.
- When asked for markdown, return ONLY the markdown body. No top-level ## header (the caller adds that). Use ### sub-headers only where instructed.

Never:
- Invent grant amounts, deadlines, or eligibility criteria not in the source data.
- Use phrases like "strong potential" or "well-positioned" without a specific supporting reason.
- Recommend applying without flagging eligibility risks present in the record.
- Pad sentences to meet length quotas."""


# ---------------------------------------------------------------------------
# Schema type markers
# ---------------------------------------------------------------------------

_STR = "str"
_LIST_STR = "list_of_str"
_BOOL_OR_PARTIAL = "bool_or_partial"
_ENUM_ABC = "enum_abc"


# ---------------------------------------------------------------------------
# Schemas for the three structured call shapes
# ---------------------------------------------------------------------------

PER_GRANT_SCHEMA: dict[str, Any] = {
    "why_client_qualifies": _STR,
    "positioning_angle": _STR,
    "what_not_to_emphasize": _LIST_STR,
    "required_documents": _LIST_STR,
    "eligibility_risks": _STR,
    "application_strategy": {
        "narrative_framework": _STR,
        "sequencing_and_dependencies": _STR,
        "common_rejection_reasons": _LIST_STR,
        "client_specific_action_list": _LIST_STR,
    },
    "notes": _STR,
}

SRED_SCHEMA: dict[str, Any] = {
    "likely_eligible": _BOOL_OR_PARTIAL,
    "reasoning": _STR,
    "eligible_activities": _LIST_STR,
    "blocking_factors": _LIST_STR,
    "interaction_with_other_grants": _STR,
    "recommended_action": _STR,
}

CTA_SCHEMA: dict[str, Any] = {
    "summary_of_delivered": _STR,
    "recommended_option": _ENUM_ABC,
    "recommendation_rationale": _STR,
    "roi_math": _STR,
}


# ---------------------------------------------------------------------------
# Schema validator (stdlib-only, recursive)
# ---------------------------------------------------------------------------

def validate_schema(obj: Any, schema: Any, path: str = "$") -> list[str]:
    """Return list of error messages; empty list == valid.

    schema may be:
      - a dict (recurse into each key)
      - _STR               -> string
      - _LIST_STR          -> list of strings
      - _BOOL_OR_PARTIAL   -> True | False | "partial"
      - _ENUM_ABC          -> "A" | "B" | "C"
    """
    errors: list[str] = []

    if isinstance(schema, dict):
        if not isinstance(obj, dict):
            errors.append(f"{path}: expected object, got {type(obj).__name__}")
            return errors
        for key, sub_schema in schema.items():
            if key not in obj:
                errors.append(f"{path}.{key}: missing")
                continue
            errors.extend(validate_schema(obj[key], sub_schema, f"{path}.{key}"))
        return errors

    if schema == _STR:
        if not isinstance(obj, str):
            errors.append(f"{path}: expected string, got {type(obj).__name__}")
    elif schema == _LIST_STR:
        if not isinstance(obj, list):
            errors.append(f"{path}: expected list, got {type(obj).__name__}")
        else:
            for i, item in enumerate(obj):
                if not isinstance(item, str):
                    errors.append(
                        f"{path}[{i}]: expected string, got {type(item).__name__}"
                    )
    elif schema == _BOOL_OR_PARTIAL:
        if obj is not True and obj is not False and obj != "partial":
            errors.append(
                f"{path}: expected true | false | 'partial', got {obj!r}"
            )
    elif schema == _ENUM_ABC:
        if obj not in {"A", "B", "C"}:
            errors.append(f"{path}: expected 'A' | 'B' | 'C', got {obj!r}")
    else:
        errors.append(f"{path}: unknown schema marker {schema!r}")

    return errors


# ---------------------------------------------------------------------------
# Sentinel factories -- populate the same shape as a successful call so
# downstream consumers (reporter.py) never break on a failed section.
# ---------------------------------------------------------------------------

def per_grant_sentinel(reason: str) -> dict:
    marker = f"<generation failed: {reason}>"
    return {
        "why_client_qualifies": marker,
        "positioning_angle": marker,
        "what_not_to_emphasize": [],
        "required_documents": [],
        "eligibility_risks": marker,
        "application_strategy": {
            "narrative_framework": marker,
            "sequencing_and_dependencies": marker,
            "common_rejection_reasons": [],
            "client_specific_action_list": [],
        },
        "notes": f"Auto-fallback: {reason}",
    }


def sred_sentinel(reason: str) -> dict:
    marker = f"<generation failed: {reason}>"
    return {
        "likely_eligible": "partial",
        "reasoning": marker,
        "eligible_activities": [],
        "blocking_factors": [],
        "interaction_with_other_grants": marker,
        "recommended_action": marker,
    }


def cta_sentinel(reason: str) -> dict:
    marker = f"<generation failed: {reason}>"
    return {
        "summary_of_delivered": marker,
        "recommended_option": "A",  # conservative default
        "recommendation_rationale": marker,
        "roi_math": marker,
    }


def freeform_sentinel(reason: str) -> str:
    return (
        f"<generation failed: {reason}. Regenerate with intelligence.py "
        f"to retry this section.>"
    )


# ---------------------------------------------------------------------------
# Prompt builders -- 7 total
#
#   1. build_system_prompt       static Directive v2.0 rules (cached block 1)
#   2. build_context_block       client profile + top-3 summary (cached block 2)
#   3. build_per_grant_task      per-grant structured JSON (calls 1-3)
#   4. build_sred_task           SR&ED structured JSON (call 4)
#   5. build_90_day_task         90-day plan freeform md, locked 5 subheaders
#                                (call 5)
#   6. build_cta_task            Stragentic CTA structured JSON (call 6)
#   7. build_diy_starter_kit_task DIY starter kit freeform md, locked 4
#                                subheaders (calls 7-9)
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    """Return the PathGrant Directive v2.0 system prompt.

    Wrapped in a function (rather than referenced as a bare constant) so
    callers can swap implementations or add dynamic substitutions later
    without touching call sites.
    """
    return SYSTEM_PROMPT


def build_context_block(client: dict, top_grants: list[dict]) -> str:
    """Shared context block used by every call in a run.

    This block is tagged for API-level caching: it is identical across all
    9 calls in a given client run, so after the first call it costs ~10%
    of its uncached tokens. The full client profile goes here; full grant
    records go into the per-grant task instructions (which are NOT cached).
    """
    if top_grants:
        top_lines = [
            f"  {i}. {r['program_name']} "
            f"(grant_id: {r['grant_id']}, score: {r['score']})"
            for i, r in enumerate(top_grants, 1)
        ]
        top_summary = "\n".join(top_lines)
    else:
        top_summary = "  (none above tier threshold)"

    return (
        "CLIENT PROFILE:\n"
        + json.dumps(client, indent=2, ensure_ascii=False)
        + "\n\nTOP-RANKED GRANTS FOR THIS CLIENT (Tier 1 and 2 only):\n"
        + top_summary
        + "\n"
    )


def build_per_grant_task(grant: dict, matcher_result: dict) -> str:
    """Per-grant structured JSON task (calls 1-3, one per top grant)."""
    score = matcher_result.get("score", 0)
    tier_label = "1" if score >= 60 else "2" if score >= 30 else "3"
    grant_json = json.dumps(grant, indent=2, ensure_ascii=False)
    signals = json.dumps(matcher_result.get("signals", []))
    penalties = json.dumps(matcher_result.get("penalties", []))
    warnings = json.dumps(matcher_result.get("warnings", []))
    return f"""TASK: Generate per-grant intelligence for the grant below, using the client profile in the previous context block.

Return ONLY this JSON. All fields mandatory; use empty string or empty array for any field that is genuinely inapplicable and explain in "notes".

{{
  "why_client_qualifies": "<3-4 sentences. Reference actual client details (org name, team members by role, location, stage, sectors). Not generic.>",
  "positioning_angle": "<3-6 sentences. What narrative wins THIS specific grant. What the funder actually cares about based on the program mandate in the grant record. How this client's differentiators map to that mandate. What language resonates.>",
  "what_not_to_emphasize": [
    "<risk 1: specific, references actual client facts>",
    "<risk 2>",
    "<risk 3>"
  ],
  "required_documents": [
    "<doc 1: reference actual client org name, board structure, incorporation status as they appear in the profile>",
    "<doc 2>"
  ],
  "eligibility_risks": "<Honest assessment. Flag real disqualifiers even if uncomfortable. Name pre-launch risks, demographic mismatches, missing incorporation, structural gaps explicitly.>",
  "application_strategy": {{
    "narrative_framework": "<Story to tell for this specific funder. Key language and framing. How to position the client's current stage.>",
    "sequencing_and_dependencies": "<Does this benefit from another approval first? What must be organizationally true before applying?>",
    "common_rejection_reasons": [
      "<reason 1, with avoidance tactic>",
      "<reason 2, with avoidance tactic>"
    ],
    "client_specific_action_list": [
      "<exact document name + who signs + timeline>",
      "<next step>"
    ]
  }},
  "notes": "<empty string unless flagging something>"
}}

GRANT RECORD:
{grant_json}

MATCHER OUTPUT FOR THIS PAIR:
score: {score}
tier: Tier {tier_label}
signals: {signals}
penalties: {penalties}
warnings: {warnings}
"""


def build_sred_task(client: dict, top_grants: list[dict]) -> str:
    """SR&ED structured JSON task (call 4, once per report)."""
    return """TASK: Assess whether this client's activities qualify for the Scientific Research and Experimental Development (SR&ED) Tax Incentive Program (federal, CRA-administered). The client profile is in the context block above.

Consider:
- Is there any biomedical, curriculum technology, athlete tracking, materials science, or systematic investigation of technological uncertainty?
- Does the client operate a for-profit entity that could claim? NFP parents cannot claim SR&ED; for-profit OpsCo entities can.
- Reference the client's EXACT entity structure from the profile.
- Flag any grants in the top-ranked list whose funding would reduce the SR&ED pool dollar-for-dollar (e.g. NRC IRAP).

Return ONLY this JSON:

{
  "likely_eligible": true | false | "partial",
  "reasoning": "<2-3 sentences explaining the call>",
  "eligible_activities": ["<activity 1>", "..."],
  "blocking_factors": ["<factor 1>", "..."],
  "interaction_with_other_grants": "<Flag top-ranked grants whose funding interacts with SR&ED (e.g. NRC IRAP). Empty string if none.>",
  "recommended_action": "<1-2 sentences: engage SR&ED consultant, defer until post-launch, not applicable, etc.>"
}
"""


def build_90_day_task(client: dict, top_grants: list[dict], today: str) -> str:
    """90-day action plan freeform markdown task (call 5, once per report).

    Sub-header labels are LOCKED. The model must not add, remove, rename,
    or reorder them.
    """
    return f"""TASK: Produce a 90-day action plan in markdown, anchored to today ({today}), for this client to pursue the top-ranked grants listed in the context block above. Reference team members by ROLE (pulled from the client profile team field). Reference documents by NAME. Include decision gates.

RETURN MARKDOWN BODY ONLY. No top-level ## header (reporter owns that).

Use EXACTLY these ### sub-headers in this order -- do not add, remove, rename, or reorder them:

### Weeks 1-4
(bullet list of action items for weeks 1 through 4)

### Weeks 5-8
(bullet list of action items for weeks 5 through 8)

### Weeks 9-12
(bullet list of action items for weeks 9 through 12)

### Decision gates
(bullet list: each gate in the form "If <trigger>, then <pivot>". Reference specific top-ranked grant names when applicable.)

### Responsible parties
(bullet list: "<Role>: <specific tasks>". Use role names from the team field in the client profile.)
"""


def build_cta_task(client: dict, top_grants: list[dict]) -> str:
    """Stragentic CTA structured JSON task (call 6, once per report)."""
    top_names = ", ".join(r["program_name"] for r in top_grants[:3]) or "(none)"
    return f"""TASK: Generate the Stragentic engagement CTA narrative for this client. The reporter template hardcodes the three engagement options and their prices -- you do NOT list them. Your job is the per-client narrative.

Pricing reference (use for ROI math only, do not echo in output):
- Option A: DIY + Review -- $1,500/grant
- Option B: Stragentic Drafts -- $3,500-5,000/grant
- Option C: Full Service -- $7,500 + 3% of awarded

Top-ranked grants for this client: {top_names}

Return ONLY this JSON:

{{
  "summary_of_delivered": "<1-2 sentences recapping what PathGrant produced: record count context, top grants, time-sensitive alerts if any>",
  "recommended_option": "A" | "B" | "C",
  "recommendation_rationale": "<2-3 sentences specific to this client's stage, budget, and top grant>",
  "roi_math": "<One sentence: 'Option C fee of $X vs top grant $Y: one approval covers this Z times over.' Use the actual top grant amount from the context block.>"
}}
"""


def build_diy_starter_kit_task(grant: dict, matcher_result: dict) -> str:
    """DIY starter kit freeform markdown task (calls 7-9, one per top grant).

    Sub-header labels are LOCKED. The model must not add, remove, rename,
    or reorder them.
    """
    grant_json = json.dumps(grant, indent=2, ensure_ascii=False)
    score = matcher_result.get("score", 0)
    penalties = json.dumps(matcher_result.get("penalties", []))
    warnings = json.dumps(matcher_result.get("warnings", []))
    return f"""TASK: Produce a DIY starter kit for this specific grant. The client is deciding whether to self-submit. Give them everything they need to actually execute this application on their own -- not a high-level overview.

This section is explicitly non-sales. Help the client succeed. Satisfied report buyers become referral sources. Instructions must be complete enough to actually execute.

RETURN MARKDOWN BODY ONLY. No top-level ## header (reporter owns that).

Use EXACTLY these ### sub-headers in this order -- do not add, remove, rename, or reorder them:

### Steps to submit
(numbered list, exact order to execute from "today" to "submitted")

### Time commitment
(ONE line: total hours AND week distribution, e.g. "20-30 hours over 4 weeks: 5 hours research, 8 hours writing, 4 hours reviews, 3-13 hours supporting documents")

### Required documents
(bulleted checklist, complete enough that a reader can gather documents from scratch without ambiguity)

### Complexity flag
(ONE honest paragraph naming what is specifically hard about THIS grant -- not generic difficulties. Traps like strict eligibility wording, sparse public guidance, committee politics, uncommon documentation, narrow scoring rubrics.)

GRANT RECORD:
{grant_json}

MATCHER OUTPUT FOR THIS PAIR:
score: {score}
penalties: {penalties}
warnings: {warnings}
"""


# ---------------------------------------------------------------------------
# Chunk 2 sanity check -- every prompt builder must return a non-empty
# string containing the expected markers. Sentinel checks from chunk 1 are
# retained so a regression in either chunk is caught by a single run.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _ok = True
    _results: list[tuple[str, bool, str]] = []

    def _check(label: str, cond: bool, detail: str = "") -> None:
        _results.append((label, cond, detail))

    # Chunk 1 sentinels still validate.
    _e = validate_schema(per_grant_sentinel("test"), PER_GRANT_SCHEMA)
    _check("per_grant sentinel matches schema", not _e, str(_e))

    _e = validate_schema(sred_sentinel("test"), SRED_SCHEMA)
    _check("sred sentinel matches schema", not _e, str(_e))

    _e = validate_schema(cta_sentinel("test"), CTA_SCHEMA)
    _check("cta sentinel matches schema", not _e, str(_e))

    _check(
        "freeform sentinel is a non-empty string",
        isinstance(freeform_sentinel("test"), str) and len(freeform_sentinel("test")) > 0,
    )

    # Chunk 2 prompt builders.
    _check(
        "build_system_prompt returns Directive v2.0 rules",
        "PathGrant Directive v2.0" in build_system_prompt(),
    )

    _fake_client = {
        "client_id": "test",
        "organization_name": "Test Org",
        "province": "SK",
        "sectors": ["youth"],
    }
    _fake_results = [
        {"grant_id": "a", "program_name": "Grant A", "score": 92},
        {"grant_id": "b", "program_name": "Grant B", "score": 70},
        {"grant_id": "c", "program_name": "Grant C", "score": 45},
    ]
    _context = build_context_block(_fake_client, _fake_results)
    _check("context block includes CLIENT PROFILE", "CLIENT PROFILE:" in _context)
    _check("context block includes TOP-RANKED GRANTS", "TOP-RANKED GRANTS" in _context)
    _check("context block lists all top-3 grant_ids",
           all(r["grant_id"] in _context for r in _fake_results))

    _fake_grant = {
        "grant_id": "a",
        "program_name": "Grant A",
        "sectors": ["youth"],
        "amount_max": 100000,
    }
    _fake_matcher = {
        "grant_id": "a",
        "program_name": "Grant A",
        "score": 92,
        "signals": [{"label": "province_eligible", "points": 20}],
        "penalties": [],
        "warnings": [],
    }
    _pg = build_per_grant_task(_fake_grant, _fake_matcher)
    _check("per_grant task includes required schema keys",
           all(k in _pg for k in (
               "why_client_qualifies", "positioning_angle",
               "what_not_to_emphasize", "required_documents",
               "eligibility_risks", "application_strategy",
               "narrative_framework", "sequencing_and_dependencies",
               "common_rejection_reasons", "client_specific_action_list",
           )))
    _check("per_grant task embeds GRANT RECORD section",
           "GRANT RECORD:" in _pg)
    _check("per_grant task embeds MATCHER OUTPUT section",
           "MATCHER OUTPUT FOR THIS PAIR:" in _pg)
    _check("per_grant task marks Tier 1 for score 92",
           "tier: Tier 1" in _pg)

    _sred = build_sred_task(_fake_client, _fake_results)
    _check("sred task mentions SR&ED", "SR&ED" in _sred)
    _check("sred task includes likely_eligible schema", '"likely_eligible"' in _sred)

    _plan = build_90_day_task(_fake_client, _fake_results, "2026-04-15")
    _LOCKED_PLAN_HEADERS = (
        "### Weeks 1-4",
        "### Weeks 5-8",
        "### Weeks 9-12",
        "### Decision gates",
        "### Responsible parties",
    )
    for _h in _LOCKED_PLAN_HEADERS:
        _check(f"90-day task locks subheader: {_h}", _h in _plan)
    _check("90-day task anchors to provided today", "2026-04-15" in _plan)
    _check("90-day task does NOT include top-level ## header",
           "\n## " not in _plan)

    _cta = build_cta_task(_fake_client, _fake_results)
    for _letter in ("A", "B", "C"):
        _check(f"cta task includes Option {_letter}", f"Option {_letter}" in _cta)
    _check("cta task schema includes recommended_option",
           '"recommended_option"' in _cta)

    _diy = build_diy_starter_kit_task(_fake_grant, _fake_matcher)
    _LOCKED_DIY_HEADERS = (
        "### Steps to submit",
        "### Time commitment",
        "### Required documents",
        "### Complexity flag",
    )
    for _h in _LOCKED_DIY_HEADERS:
        _check(f"DIY task locks subheader: {_h}", _h in _diy)
    _check("DIY task embeds GRANT RECORD section", "GRANT RECORD:" in _diy)

    # Summary
    for _label, _cond, _detail in _results:
        _marker = "PASS" if _cond else "FAIL"
        print(f"[{_marker}] {_label}")
        if not _cond and _detail:
            print(f"    {_detail}")
        _ok = _ok and _cond

    _total = len(_results)
    _passed = sum(1 for _, c, _d in _results if c)
    print("=" * 60)
    print(f"CHUNK 2 OK ({_passed}/{_total})" if _ok else f"CHUNK 2 FAILED ({_passed}/{_total})")
    sys.exit(0 if _ok else 1)
