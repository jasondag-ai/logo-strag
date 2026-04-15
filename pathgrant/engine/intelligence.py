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
import random
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

Prose style rules (operator tone, not consultant-speak):

9. Do not use em dashes. Replace with a comma, period, colon, or rewrite the sentence.
10. Do not use the construction "this is not X, it is Y" or "not X but Y" as a rhetorical device. State the positive directly.
11. Do not use "it is worth noting", "it is important to", "this is critical", or similar throat-clearing phrases. State the point.
12. Do not use "robust", "nuanced", "holistic", "leverage" (as a verb), or "ecosystem" unless quoting funder language directly.
13. Write in plain declarative sentences.

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


def _days_between(today_str: str, date_str: str) -> int | None:
    """Return integer days from today_str to date_str (both YYYY-MM-DD).

    Returns None if either date fails to parse. Negative values mean the
    target date is in the past.
    """
    try:
        today_d = datetime.strptime(today_str, "%Y-%m-%d").date()
        target_d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (target_d - today_d).days


def _amount_midpoint(grant: dict) -> int | None:
    """Return a realistic midpoint for a grant's award amount, in dollars.

    Uses amount_min and amount_max from the grant record. Returns None if
    neither is set (amount not publicly stated).
    """
    lo = grant.get("amount_min")
    hi = grant.get("amount_max")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        return int((lo + hi) / 2)
    if isinstance(hi, (int, float)):
        return int(hi)
    if isinstance(lo, (int, float)):
        return int(lo)
    return None


def build_cta_task(
    client: dict,
    top_grants: list[dict],
    top_grant_records: list[dict],
    today_str: str,
) -> str:
    """Stragentic CTA structured JSON task (call 6, once per report).

    Injects a deadline block for any time-sensitive top grant, and a top-grant
    amount block so the model can compute concrete ROI math. The prompt
    builder is responsible for surfacing this data, not the model.
    """
    top_names = ", ".join(r["program_name"] for r in top_grants[:3]) or "(none)"

    # --- Deadline injection block ---
    deadline_lines: list[str] = []
    for r, g in zip(top_grants, top_grant_records):
        if g.get("time_sensitive") is not True:
            continue
        close = g.get("intake_close_date") or "deadline TBD"
        days = _days_between(today_str, close) if close != "deadline TBD" else None
        if days is not None:
            days_phrase = f"{days} days from today ({today_str})"
        else:
            days_phrase = "deadline date not parseable"
        deadline_lines.append(
            f"- {r['program_name']}: close {close}, {days_phrase}"
        )
    if deadline_lines:
        deadline_block = (
            "TIME-SENSITIVE GRANTS IN THIS REPORT (you MUST reference these "
            "in summary_of_delivered and recommendation_rationale; you MUST "
            "NOT claim 'no immediate hard deadlines'):\n"
            + "\n".join(deadline_lines)
        )
    else:
        deadline_block = (
            "TIME-SENSITIVE GRANTS IN THIS REPORT: none. "
            "You may state that no immediate hard deadlines were flagged."
        )

    # --- Top-grant amount block for concrete ROI math ---
    if top_grant_records:
        top_grant = top_grant_records[0]
        top_grant_name = top_grants[0]["program_name"]
        mid = _amount_midpoint(top_grant)
        amount_notes = top_grant.get("amount_notes") or ""
        if mid is not None:
            fee_c = 7500 + int(mid * 0.03)
            multiple = mid // fee_c if fee_c > 0 else 0
            amount_block = (
                f"TOP GRANT AMOUNT DATA (use for ROI math):\n"
                f"- Top grant name: {top_grant_name}\n"
                f"- amount_min: {top_grant.get('amount_min')}\n"
                f"- amount_max: {top_grant.get('amount_max')}\n"
                f"- amount_notes: {amount_notes}\n"
                f"- Realistic midpoint: ${mid:,}\n"
                f"- Option C fee at midpoint: $7,500 + 3% of ${mid:,} = "
                f"${fee_c:,}\n"
                f"- Return multiple: ${mid:,} / ${fee_c:,} = ~{multiple}x"
            )
        else:
            amount_block = (
                f"TOP GRANT AMOUNT DATA:\n"
                f"- Top grant name: {top_grant_name}\n"
                f"- amount_min: {top_grant.get('amount_min')}\n"
                f"- amount_max: {top_grant.get('amount_max')}\n"
                f"- amount_notes: {amount_notes}\n"
                f"- Amount not publicly stated. "
                f"ROI math must say so explicitly and cite amount_notes. "
                f"Do not invent a dollar figure."
            )
    else:
        top_grant_name = "(none)"
        amount_block = "TOP GRANT AMOUNT DATA: no top grants supplied."

    return f"""TASK: Generate the Stragentic engagement CTA narrative for this client. The reporter template hardcodes the three engagement options and their prices; you do NOT list them. Your job is the per-client narrative.

Pricing reference (use for ROI math only, do not echo in output):
- Option A: DIY + Review, $1,500/grant
- Option B: Stragentic Drafts, $3,500 to $5,000/grant
- Option C: Full Service, $7,500 + 3% of awarded

Top-ranked grants for this client: {top_names}

{deadline_block}

{amount_block}

ROI math format requirement:
Write roi_math using the actual numbers above. Required format:
"Option C fee on {top_grant_name} is $7,500 plus 3% of awarded. If awarded $<realistic midpoint from block above>, total fee is $<calculated fee>, against $<award amount> received, ~<X>x return."
If the amount is not publicly stated, state that explicitly in roi_math and cite amount_notes. Do not use vague language like "returns the engagement cost many times over" or "covers the fee several times".

Return ONLY this JSON:

{{
  "summary_of_delivered": "<1-2 sentences recapping what PathGrant produced: record count context, top grants, time-sensitive deadlines by name with days remaining if any exist>",
  "recommended_option": "A" | "B" | "C",
  "recommendation_rationale": "<2-3 sentences specific to this client's stage, budget, top grant, and any time-sensitive deadlines surfaced above>",
  "roi_math": "<One sentence in the required format above, with actual calculated numbers>"
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
# Parser -- strip optional code fences, json.loads, validate against schema
# ---------------------------------------------------------------------------

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def strip_code_fences(text: str) -> str:
    """Strip optional ```json``` (or plain ```) wrapping from a response."""
    stripped = text.strip()
    m = _CODE_FENCE_RE.match(stripped)
    if m:
        return m.group(1).strip()
    return stripped


def parse_json_response(
    text: str, schema: dict
) -> tuple[dict | None, str | None]:
    """Return (parsed_obj, None) on success, (None, error_message) on
    parse failure OR schema violation."""
    body = strip_code_fences(text)
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        return None, f"JSON parse error: {exc}"
    errors = validate_schema(obj, schema)
    if errors:
        return None, "Schema validation errors: " + "; ".join(errors)
    return obj, None


# ---------------------------------------------------------------------------
# Anthropic API client (deferred import so dry-run / tests / reporter
# import work without the SDK installed)
# ---------------------------------------------------------------------------

_anthropic_client_cache: Any = None


def _get_anthropic_client():
    """Lazily import the anthropic SDK and cache a client instance.

    Raises RuntimeError if called without the SDK installed or without
    ANTHROPIC_API_KEY in the environment. Module-level import is
    deferred so dry-run mode, inline tests, and reporter.py imports all
    work even when the SDK is not present.
    """
    global _anthropic_client_cache
    if _anthropic_client_cache is not None:
        return _anthropic_client_cache
    try:
        from anthropic import Anthropic  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "anthropic SDK not installed. pip install anthropic"
        ) from exc
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable not set"
        )
    _anthropic_client_cache = Anthropic()
    return _anthropic_client_cache


def call_anthropic(
    *,
    system: str,
    context_block: str,
    task_instruction: str,
    model: str,
    temperature: float,
    max_tokens: int,
) -> tuple[str, dict]:
    """Make one Claude API call with prompt caching. Returns
    (response_text, call_metadata).

    The system prompt and context block are tagged with ephemeral
    cache_control so they are reused across every call in a run. The
    task-specific instruction is the only uncached portion.
    """
    client = _get_anthropic_client()
    start = time.monotonic()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=[
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": context_block,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": task_instruction,
                    },
                ],
            }
        ],
    )
    latency_ms = int((time.monotonic() - start) * 1000)

    parts: list[str] = []
    for block in response.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    text = "".join(parts)

    usage = getattr(response, "usage", None)
    metadata = {
        "latency_ms": latency_ms,
        "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
        "cache_creation_input_tokens":
            getattr(usage, "cache_creation_input_tokens", None) if usage else None,
        "cache_read_input_tokens":
            getattr(usage, "cache_read_input_tokens", None) if usage else None,
    }
    return text, metadata


# ---------------------------------------------------------------------------
# Retry / backoff logic
# ---------------------------------------------------------------------------

def call_with_backoff(
    *,
    system: str,
    context_block: str,
    task_instruction: str,
    model: str,
    temperature: float,
    max_tokens: int,
    _api_fn: Callable | None = None,
    _sleep_fn: Callable | None = None,
) -> tuple[str | None, dict, str | None]:
    """Call the API with exponential backoff + jitter on transient failures.

    Retries up to MAX_API_RETRIES times. Delay schedule: (2**attempt) + jitter
    seconds, i.e. ~1s, ~2s, ~4s with a [0, 1)s random jitter added.

    Returns (response_text, metadata, error_message). On total failure
    response_text is None and error_message is set to the last exception.

    _api_fn defaults to call_anthropic; tests inject mock functions.
    _sleep_fn defaults to time.sleep; tests inject a no-op to keep test
    runtime fast.
    """
    api_fn = _api_fn or call_anthropic
    sleep_fn = _sleep_fn or time.sleep

    last_error: str | None = None
    metadata: dict = {}
    for attempt in range(MAX_API_RETRIES):
        try:
            text, metadata = api_fn(
                system=system,
                context_block=context_block,
                task_instruction=task_instruction,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return text, metadata, None
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_API_RETRIES - 1:
                delay = (2 ** attempt) + random.uniform(0, 1)
                sleep_fn(delay)
    return None, metadata, last_error


# ---------------------------------------------------------------------------
# Section wrappers -- separate paths for structured (JSON) vs freeform
# (markdown) so freeform calls never hit false JSON parse failures.
# ---------------------------------------------------------------------------

def generate_structured_section(
    *,
    system: str,
    context_block: str,
    task_instruction: str,
    schema: dict,
    sentinel_factory: Callable[[str], dict],
    model: str,
    temperature: float,
    max_tokens: int,
    dry_run: bool,
    _api_fn: Callable | None = None,
    _sleep_fn: Callable | None = None,
) -> tuple[dict, dict]:
    """Generate a structured (JSON) section. Parse-retry once with a
    stricter reprompt on parse / schema failure, then fall through to a
    sentinel so the downstream report template never breaks.

    Returns (parsed_obj_or_sentinel, call_record).
    """
    call_record: dict = {
        "type": "structured",
        "success": False,
        "parse_retries": 0,
        "api_attempts": 0,
        "error": None,
        "latency_ms": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
    }

    if dry_run:
        _print_dryrun_prompt("STRUCTURED", system, context_block, task_instruction)
        call_record["success"] = True
        call_record["dry_run"] = True
        return sentinel_factory("dry_run preview"), call_record

    current_instruction = task_instruction
    for parse_attempt in range(MAX_PARSE_RETRIES + 1):
        call_record["api_attempts"] += 1
        text, meta, err = call_with_backoff(
            system=system,
            context_block=context_block,
            task_instruction=current_instruction,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            _api_fn=_api_fn,
            _sleep_fn=_sleep_fn,
        )
        for k in (
            "latency_ms", "input_tokens", "output_tokens",
            "cache_creation_input_tokens", "cache_read_input_tokens",
        ):
            if k in meta:
                call_record[k] = meta[k]
        if err:
            call_record["error"] = f"api_error: {err}"
            return sentinel_factory(err), call_record

        obj, parse_err = parse_json_response(text, schema)
        if obj is not None:
            call_record["success"] = True
            return obj, call_record

        call_record["parse_retries"] += 1
        if parse_attempt < MAX_PARSE_RETRIES:
            current_instruction = (
                task_instruction
                + f"\n\nNOTE: Previous response failed validation: {parse_err}\n"
                "Return ONLY valid JSON matching the schema above. "
                "No preamble, no code fences, no prose wrapper."
            )
        else:
            call_record["error"] = f"parse_error: {parse_err}"
            return sentinel_factory(parse_err or "parse error"), call_record

    return sentinel_factory("max retries exceeded"), call_record


def generate_freeform_section(
    *,
    system: str,
    context_block: str,
    task_instruction: str,
    model: str,
    temperature: float,
    max_tokens: int,
    dry_run: bool,
    _api_fn: Callable | None = None,
    _sleep_fn: Callable | None = None,
) -> tuple[str, dict]:
    """Generate a freeform (markdown) section.

    Never hits JSON parse failures -- the response is taken as-is (after
    stripping any code-fence wrapper) and embedded in the output. Only
    true API-level failures (network, rate limit after retries) can
    fail this call, in which case a sentinel string is returned.

    Returns (markdown_body_or_sentinel, call_record).
    """
    call_record: dict = {
        "type": "freeform",
        "success": False,
        "parse_retries": 0,
        "api_attempts": 0,
        "error": None,
        "latency_ms": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
    }

    if dry_run:
        _print_dryrun_prompt("FREEFORM", system, context_block, task_instruction)
        call_record["success"] = True
        call_record["dry_run"] = True
        return "<dry_run preview>", call_record

    call_record["api_attempts"] = 1
    text, meta, err = call_with_backoff(
        system=system,
        context_block=context_block,
        task_instruction=task_instruction,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        _api_fn=_api_fn,
        _sleep_fn=_sleep_fn,
    )
    for k in (
        "latency_ms", "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
    ):
        if k in meta:
            call_record[k] = meta[k]
    if err:
        call_record["error"] = f"api_error: {err}"
        return freeform_sentinel(err), call_record

    body = strip_code_fences(text)
    call_record["success"] = True
    return body, call_record


def _print_dryrun_prompt(
    kind: str, system: str, context_block: str, task_instruction: str
) -> None:
    """Pretty-print a prompt for dry-run review. Stdout only, no file IO."""
    sep = "=" * 72
    print(sep)
    print(f"DRY RUN -- {kind} CALL")
    print(sep)
    print("--- system (cached) ---")
    print(system)
    print("\n--- context block (cached) ---")
    truncated = context_block[:2000]
    if len(context_block) > 2000:
        truncated += "\n...[truncated]"
    print(truncated)
    print("\n--- task instruction ---")
    print(task_instruction)
    print(sep)
    print()


# ---------------------------------------------------------------------------
# Top-N selection + orchestrator + CLI (chunk 4)
# ---------------------------------------------------------------------------

def select_top_grants(
    scored: list[dict], n: int = DEFAULT_TOP_N
) -> list[dict]:
    """Pick top N scored grants by score globally.

    Filters: score >= MIN_TIER_SCORE (drops Tier 3) AND eliminator is
    not set (drops province_not_eligible and operator_flag records).
    Sorts by score descending before trimming -- defensive against
    callers that pass unsorted input; matcher.match() already returns
    sorted results so this is a no-op in the normal flow.
    """
    filtered = [
        r for r in scored
        if r.get("score", 0) >= MIN_TIER_SCORE
        and not r.get("eliminator")
    ]
    filtered.sort(key=lambda r: -r.get("score", 0))
    return filtered[:n]


def generate_intelligence(
    client_id: str,
    *,
    top_n: int = DEFAULT_TOP_N,
    verified_path: Path = DEFAULT_VERIFIED_PATH,
    unverified_path: Path = DEFAULT_UNVERIFIED_PATH,
    intelligence_root: Path = DEFAULT_INTELLIGENCE_ROOT,
    clients_root: Path = DEFAULT_CLIENTS_ROOT,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    dry_run: bool = False,
    write_files: bool = True,
    today: str | None = None,
    _api_fn: Callable | None = None,
    _sleep_fn: Callable | None = None,
) -> dict:
    """Generate narrative intelligence for one client.

    Runs 9 Claude API calls (for top_n=3) in this order:
      1-3: per-grant structured (one per top grant)
      4:   SR&ED assessment (structured)
      5:   90-day action plan (freeform markdown)
      6:   Stragentic CTA (structured)
      7-9: DIY starter kit (freeform markdown, one per top grant)

    On a per-call failure the orchestrator writes a sentinel into the
    corresponding section and logs metadata.api_failures, then continues
    with the remaining calls. Resilient completion is always attempted.

    When write_files=True and dry_run=False, the full intelligence dict
    is written to both:
        pathgrant/intelligence/<client_id>/<timestamp>.json
        pathgrant/intelligence/<client_id>/latest.json

    When dry_run=True the call graph runs with generate_*_section's
    dry-run branch -- which prints the prompts to stdout via
    _print_dryrun_prompt() and makes zero API calls -- and the output
    is returned without writing files.
    """
    # Deferred matcher import keeps the chunk-3-only sanity check clean of
    # cross-module dependencies; matcher only matters inside the full
    # orchestrator run.
    from engine.matcher import load_client_profile, load_grants, match

    generated_at = datetime.now(timezone.utc)
    today_str = today or generated_at.strftime("%Y-%m-%d")

    client_path = clients_root / f"{client_id}_profile.json"
    client = load_client_profile(client_path)

    verified = load_grants(verified_path)
    _unverified = load_grants(unverified_path)  # reserved for future sections

    scored = match(client, verified)
    top_results = select_top_grants(scored, n=top_n)
    grant_lookup = {g["grant_id"]: g for g in verified}
    top_grants_records = [grant_lookup[r["grant_id"]] for r in top_results]

    system = build_system_prompt()
    context_block = build_context_block(client, top_results)

    out: dict = {
        "metadata": {
            "client_id": client_id,
            "generated_at": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "directive_version": DIRECTIVE_VERSION,
            "top_n": top_n,
            "top_grants_covered": [r["grant_id"] for r in top_results],
            "api_calls_total": 0,
            "api_call_log": [],
            "api_failures": [],
            "dry_run": dry_run,
        },
        "per_grant": {},
        "per_client": {
            "sred_assessment": None,
            "90_day_action_plan": None,
            "stragentic_cta": None,
        },
    }

    # Shared kwargs for every call.
    common = {
        "system": system,
        "context_block": context_block,
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "dry_run": dry_run,
        "_api_fn": _api_fn,
        "_sleep_fn": _sleep_fn,
    }

    call_counter = 0

    def _record_call(rec: dict, section: str, grant_id: str | None = None) -> None:
        nonlocal call_counter
        call_counter += 1
        rec["call"] = call_counter
        rec["section"] = section
        if grant_id:
            rec["grant_id"] = grant_id
        out["metadata"]["api_call_log"].append(rec)
        if not rec.get("success"):
            failure = {
                "call": call_counter,
                "section": section,
                "error": rec.get("error"),
            }
            if grant_id:
                failure["grant_id"] = grant_id
            out["metadata"]["api_failures"].append(failure)

    # --- Calls 1-3: per-grant structured ---
    for grant_result, grant_record in zip(top_results, top_grants_records):
        gid = grant_result["grant_id"]
        obj, rec = generate_structured_section(
            **common,
            task_instruction=build_per_grant_task(grant_record, grant_result),
            schema=PER_GRANT_SCHEMA,
            sentinel_factory=per_grant_sentinel,
        )
        _record_call(rec, "per_grant_structured", gid)
        out["per_grant"][gid] = {"structured": obj, "diy_starter_kit": None}

    # --- Call 4: SR&ED assessment ---
    obj, rec = generate_structured_section(
        **common,
        task_instruction=build_sred_task(client, top_results),
        schema=SRED_SCHEMA,
        sentinel_factory=sred_sentinel,
    )
    _record_call(rec, "sred_assessment")
    out["per_client"]["sred_assessment"] = obj

    # --- Call 5: 90-day action plan (freeform) ---
    md, rec = generate_freeform_section(
        **common,
        task_instruction=build_90_day_task(client, top_results, today_str),
    )
    _record_call(rec, "90_day_action_plan")
    out["per_client"]["90_day_action_plan"] = md

    # --- Call 6: Stragentic CTA ---
    obj, rec = generate_structured_section(
        **common,
        task_instruction=build_cta_task(
            client, top_results, top_grants_records, today_str
        ),
        schema=CTA_SCHEMA,
        sentinel_factory=cta_sentinel,
    )
    _record_call(rec, "stragentic_cta")
    out["per_client"]["stragentic_cta"] = obj

    # --- Calls 7-9: DIY starter kit (freeform) ---
    for grant_result, grant_record in zip(top_results, top_grants_records):
        gid = grant_result["grant_id"]
        md, rec = generate_freeform_section(
            **common,
            task_instruction=build_diy_starter_kit_task(grant_record, grant_result),
        )
        _record_call(rec, "diy_starter_kit", gid)
        out["per_grant"][gid]["diy_starter_kit"] = md

    out["metadata"]["api_calls_total"] = call_counter

    # --- File outputs ---
    if write_files and not dry_run:
        client_root = intelligence_root / client_id
        client_root.mkdir(parents=True, exist_ok=True)
        ts = generated_at.strftime("%Y%m%d-%H%M")
        timestamped_path = client_root / f"{ts}.json"
        latest_path = client_root / "latest.json"
        body = json.dumps(out, indent=2, ensure_ascii=False) + "\n"
        timestamped_path.write_text(body, encoding="utf-8")
        latest_path.write_text(body, encoding="utf-8")
        out["metadata"]["timestamped_path"] = str(
            timestamped_path.relative_to(_PATHGRANT_ROOT.parent)
        )
        out["metadata"]["latest_path"] = str(
            latest_path.relative_to(_PATHGRANT_ROOT.parent)
        )

    return out


def _cli() -> None:
    """Argparse CLI entry point. Wraps generate_intelligence() with knobs
    for every operationally relevant parameter, plus --dry-run and
    --stdout modes that never touch disk."""
    parser = argparse.ArgumentParser(
        description="Generate PathGrant narrative intelligence via Claude API"
    )
    parser.add_argument("--client", required=True,
                        help="client_id (e.g. emerge_academy)")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--verified-path", type=Path,
                        default=DEFAULT_VERIFIED_PATH)
    parser.add_argument("--unverified-path", type=Path,
                        default=DEFAULT_UNVERIFIED_PATH)
    parser.add_argument("--intelligence-root", type=Path,
                        default=DEFAULT_INTELLIGENCE_ROOT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print all 9 prompts, make zero API calls, write zero files",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Write intelligence JSON to stdout instead of disk",
    )
    parser.add_argument(
        "--today",
        default=None,
        help="Override 'today' date for 90-day plan (YYYY-MM-DD)",
    )
    args = parser.parse_args()

    out = generate_intelligence(
        client_id=args.client,
        top_n=args.top_n,
        verified_path=args.verified_path,
        unverified_path=args.unverified_path,
        intelligence_root=args.intelligence_root,
        model=args.model,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        dry_run=args.dry_run,
        write_files=not (args.stdout or args.dry_run),
        today=args.today,
    )

    # Failure summary is always shown last so it is impossible to miss.
    print()
    print("=" * 72)
    print("FAILURE SUMMARY")
    print("=" * 72)
    failures = out["metadata"]["api_failures"]
    total = out["metadata"]["api_calls_total"]
    if failures:
        for f in failures:
            gid_str = f" grant={f['grant_id']}" if f.get("grant_id") else ""
            print(
                f"  FAIL call {f['call']} ({f['section']}){gid_str}: "
                f"{f.get('error', '?')}"
            )
        print(f"\n  Total: {len(failures)}/{total} calls failed")
    else:
        print(f"  All {total} calls completed "
              f"({'dry-run, no API calls made' if args.dry_run else 'success'}).")
    print()

    if args.stdout:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        # Default and dry-run: show metadata only, not the full sentinel dump
        print(json.dumps(out["metadata"], indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Chunk 4 sanity check -- chunks 1, 2, 3, and 4 all verified in one run.
# ---------------------------------------------------------------------------

def _run_sanity_tests() -> int:
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

    _fake_grant_records = [
        {
            "grant_id": "a",
            "program_name": "Grant A",
            "time_sensitive": True,
            "intake_close_date": "2026-05-31",
            "amount_min": 50000,
            "amount_max": 200000,
            "amount_notes": "Confirmed range per funder.",
        },
        {
            "grant_id": "b",
            "program_name": "Grant B",
            "time_sensitive": False,
            "intake_close_date": None,
            "amount_min": None,
            "amount_max": None,
            "amount_notes": "Amount not publicly stated.",
        },
        {
            "grant_id": "c",
            "program_name": "Grant C",
            "time_sensitive": False,
            "intake_close_date": None,
            "amount_min": None,
            "amount_max": None,
            "amount_notes": "",
        },
    ]
    _cta = build_cta_task(
        _fake_client, _fake_results, _fake_grant_records, "2026-04-15"
    )
    for _letter in ("A", "B", "C"):
        _check(f"cta task includes Option {_letter}", f"Option {_letter}" in _cta)
    _check("cta task schema includes recommended_option",
           '"recommended_option"' in _cta)
    _check("cta task injects time-sensitive deadline block",
           "TIME-SENSITIVE GRANTS IN THIS REPORT" in _cta
           and "Grant A" in _cta
           and "2026-05-31" in _cta)
    _check("cta task computes days remaining for time-sensitive grant",
           "46 days from today (2026-04-15)" in _cta)
    _check("cta task bans 'no immediate hard deadlines' when deadlines exist",
           "you MUST NOT claim 'no immediate hard deadlines'" in _cta)
    _check("cta task injects top grant amount block with midpoint",
           "TOP GRANT AMOUNT DATA" in _cta and "$125,000" in _cta)
    _check("cta task computes concrete Option C fee",
           "$7,500 + 3% of $125,000 = $11,250" in _cta)
    _check("cta task bans vague ROI language",
           "Do not use vague language" in _cta)

    # CTA with no time-sensitive grants: allows the "none" clause.
    _fake_records_no_deadline = [
        {**r, "time_sensitive": False, "intake_close_date": None}
        for r in _fake_grant_records
    ]
    _cta_none = build_cta_task(
        _fake_client, _fake_results, _fake_records_no_deadline, "2026-04-15"
    )
    _check("cta task handles no time-sensitive grants",
           "TIME-SENSITIVE GRANTS IN THIS REPORT: none" in _cta_none)

    # CTA with unknown top-grant amount: ROI math must flag explicitly.
    _fake_records_no_amount = [
        {**r, "amount_min": None, "amount_max": None,
         "amount_notes": "Amount not publicly stated."}
        for r in _fake_grant_records
    ]
    _cta_noamt = build_cta_task(
        _fake_client, _fake_results, _fake_records_no_amount, "2026-04-15"
    )
    _check("cta task handles unknown top-grant amount",
           "Amount not publicly stated" in _cta_noamt
           and "Do not invent a dollar figure" in _cta_noamt)

    # _days_between: core date arithmetic used by the deadline block.
    _check("_days_between future date returns positive days",
           _days_between("2026-04-15", "2026-05-31") == 46)
    _check("_days_between past date returns negative",
           _days_between("2026-04-15", "2026-04-01") == -14)
    _check("_days_between bad format returns None",
           _days_between("2026-04-15", "not-a-date") is None)

    # _amount_midpoint: used for ROI calculations.
    _check("_amount_midpoint with min and max returns midpoint",
           _amount_midpoint({"amount_min": 50000, "amount_max": 200000}) == 125000)
    _check("_amount_midpoint with only max returns max",
           _amount_midpoint({"amount_min": None, "amount_max": 100000}) == 100000)
    _check("_amount_midpoint with no amounts returns None",
           _amount_midpoint({"amount_min": None, "amount_max": None}) is None)

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

    # --- Chunk 3: parser ---
    _check("strip_code_fences bare JSON",
           strip_code_fences('{"x":1}') == '{"x":1}')
    _check("strip_code_fences ```json wrap",
           strip_code_fences('```json\n{"x":1}\n```') == '{"x":1}')
    _check("strip_code_fences ``` wrap (no language)",
           strip_code_fences('```\n{"x":1}\n```') == '{"x":1}')
    _check("strip_code_fences trims outer whitespace",
           strip_code_fences('  {"x":1}\n') == '{"x":1}')

    _good_json = json.dumps(per_grant_sentinel("ok"))
    _obj, _err = parse_json_response(_good_json, PER_GRANT_SCHEMA)
    _check("parse_json_response valid returns (obj, None)",
           _obj is not None and _err is None)

    _obj, _err = parse_json_response("not json at all", PER_GRANT_SCHEMA)
    _check("parse_json_response malformed returns (None, error)",
           _obj is None and _err is not None and "parse error" in _err)

    _obj, _err = parse_json_response('{"x": 1}', PER_GRANT_SCHEMA)
    _check("parse_json_response schema mismatch returns (None, error)",
           _obj is None and _err is not None and "missing" in _err)

    _fenced = f'```json\n{_good_json}\n```'
    _obj, _err = parse_json_response(_fenced, PER_GRANT_SCHEMA)
    _check("parse_json_response strips code fence + parses",
           _obj is not None and _err is None)

    # --- Chunk 3: backoff (mocked API fn) ---
    def _mock_ok(**kwargs):
        return ("ok", {"latency_ms": 10, "input_tokens": 100, "output_tokens": 50})

    _text, _meta, _err = call_with_backoff(
        system="s", context_block="c", task_instruction="t",
        model="m", temperature=0.3, max_tokens=100,
        _api_fn=_mock_ok, _sleep_fn=lambda _s: None,
    )
    _check("backoff happy path returns text",
           _text == "ok" and _err is None)

    _fail_count = [0]

    def _mock_retry_then_ok(**kwargs):
        _fail_count[0] += 1
        if _fail_count[0] < 3:
            raise RuntimeError(f"transient {_fail_count[0]}")
        return ("after retry", {"latency_ms": 10})

    _text, _meta, _err = call_with_backoff(
        system="s", context_block="c", task_instruction="t",
        model="m", temperature=0.3, max_tokens=100,
        _api_fn=_mock_retry_then_ok, _sleep_fn=lambda _s: None,
    )
    _check("backoff retries to success",
           _text == "after retry" and _err is None)
    _check("backoff attempted exactly 3 times", _fail_count[0] == 3)

    def _mock_always_fail(**kwargs):
        raise RuntimeError("permanent")

    _text, _meta, _err = call_with_backoff(
        system="s", context_block="c", task_instruction="t",
        model="m", temperature=0.3, max_tokens=100,
        _api_fn=_mock_always_fail, _sleep_fn=lambda _s: None,
    )
    _check("backoff all-fail returns None + error",
           _text is None and _err is not None and "permanent" in _err)

    # --- Chunk 3: generate_structured_section ---
    _good_pg_json = json.dumps(per_grant_sentinel("mock"))

    def _mock_struct_ok(**kwargs):
        return (_good_pg_json, {"latency_ms": 10})

    _obj, _record = generate_structured_section(
        system="s", context_block="c", task_instruction="t",
        schema=PER_GRANT_SCHEMA, sentinel_factory=per_grant_sentinel,
        model="m", temperature=0.3, max_tokens=100, dry_run=False,
        _api_fn=_mock_struct_ok, _sleep_fn=lambda _s: None,
    )
    _check("structured section happy success=True",
           _record.get("success") is True)
    _check("structured section happy parse_retries=0",
           _record.get("parse_retries") == 0)

    _parse_count = [0]

    def _mock_bad_then_ok(**kwargs):
        _parse_count[0] += 1
        if _parse_count[0] == 1:
            return ("not json", {"latency_ms": 10})
        return (_good_pg_json, {"latency_ms": 10})

    _obj, _record = generate_structured_section(
        system="s", context_block="c", task_instruction="t",
        schema=PER_GRANT_SCHEMA, sentinel_factory=per_grant_sentinel,
        model="m", temperature=0.3, max_tokens=100, dry_run=False,
        _api_fn=_mock_bad_then_ok, _sleep_fn=lambda _s: None,
    )
    _check("structured section parse-fail-then-retry succeeds",
           _record.get("success") is True)
    _check("structured section retried parse once",
           _record.get("parse_retries") == 1)

    def _mock_always_bad(**kwargs):
        return ("still not json", {"latency_ms": 10})

    _obj, _record = generate_structured_section(
        system="s", context_block="c", task_instruction="t",
        schema=PER_GRANT_SCHEMA, sentinel_factory=per_grant_sentinel,
        model="m", temperature=0.3, max_tokens=100, dry_run=False,
        _api_fn=_mock_always_bad, _sleep_fn=lambda _s: None,
    )
    _check("structured section double-parse-fail yields sentinel",
           _record.get("success") is False)
    _check("sentinel from double-parse-fail still validates schema",
           not validate_schema(_obj, PER_GRANT_SCHEMA))

    # --- Chunk 3: generate_freeform_section ---
    def _mock_freeform_ok(**kwargs):
        return ("### Weeks 1-4\n- action item", {"latency_ms": 10})

    _md, _record = generate_freeform_section(
        system="s", context_block="c", task_instruction="t",
        model="m", temperature=0.3, max_tokens=100, dry_run=False,
        _api_fn=_mock_freeform_ok, _sleep_fn=lambda _s: None,
    )
    _check("freeform section happy returns md", "Weeks 1-4" in _md)
    _check("freeform section happy success=True",
           _record.get("success") is True)

    def _mock_freeform_fail(**kwargs):
        raise RuntimeError("api down")

    _md, _record = generate_freeform_section(
        system="s", context_block="c", task_instruction="t",
        model="m", temperature=0.3, max_tokens=100, dry_run=False,
        _api_fn=_mock_freeform_fail, _sleep_fn=lambda _s: None,
    )
    _check("freeform section api-fail yields sentinel string",
           "<generation failed" in _md)
    _check("freeform section api-fail success=False",
           _record.get("success") is False)

    _check("_get_anthropic_client is callable",
           callable(_get_anthropic_client))

    # --- Chunk 4: select_top_grants ---
    _scored = [
        {"grant_id": "a", "score": 90, "eliminator": None},
        {"grant_id": "b", "score": 20, "eliminator": None},   # Tier 3 -> drop
        {"grant_id": "c", "score": 70,
         "eliminator": "province_not_eligible"},              # eliminated -> drop
        {"grant_id": "d", "score": 50, "eliminator": None},
        {"grant_id": "e", "score": 45, "eliminator": None},
        {"grant_id": "f", "score": 60, "eliminator": None},
    ]
    _top = select_top_grants(_scored, n=3)
    _check("select_top_grants drops Tier 3 and eliminated records",
           [r["grant_id"] for r in _top] == ["a", "f", "d"],
           detail=str([r["grant_id"] for r in _top]))
    _check("select_top_grants respects n=2",
           len(select_top_grants(_scored, n=2)) == 2)
    _check("select_top_grants handles empty input",
           select_top_grants([], n=3) == [])
    _check("select_top_grants returns list not generator",
           isinstance(select_top_grants(_scored, n=3), list))

    _check("generate_intelligence is callable", callable(generate_intelligence))
    _check("_cli is callable", callable(_cli))

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
    print(f"CHUNK 4 OK ({_passed}/{_total})" if _ok else f"CHUNK 4 FAILED ({_passed}/{_total})")
    return 0 if _ok else 1


# ---------------------------------------------------------------------------
# Dispatch: --test runs the inline sanity suite, otherwise run the CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.argv.remove("--test")
        sys.exit(_run_sanity_tests())
    else:
        _cli()
