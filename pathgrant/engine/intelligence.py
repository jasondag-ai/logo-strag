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
# Chunk 1 sanity check -- every sentinel must validate against its schema.
# Remaining chunks (prompt builders, parser/API client, orchestrator/CLI)
# will be appended in follow-up commits.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _ok = True

    _e = validate_schema(per_grant_sentinel("test"), PER_GRANT_SCHEMA)
    print(f"[{'PASS' if not _e else 'FAIL'}] per_grant sentinel matches schema: {_e}")
    _ok = _ok and not _e

    _e = validate_schema(sred_sentinel("test"), SRED_SCHEMA)
    print(f"[{'PASS' if not _e else 'FAIL'}] sred sentinel matches schema: {_e}")
    _ok = _ok and not _e

    _e = validate_schema(cta_sentinel("test"), CTA_SCHEMA)
    print(f"[{'PASS' if not _e else 'FAIL'}] cta sentinel matches schema: {_e}")
    _ok = _ok and not _e

    _s = freeform_sentinel("test")
    _freeform_ok = isinstance(_s, str) and len(_s) > 0
    print(f"[{'PASS' if _freeform_ok else 'FAIL'}] freeform sentinel is a non-empty string")
    _ok = _ok and _freeform_ok

    print("=" * 60)
    print("CHUNK 1 OK" if _ok else "CHUNK 1 FAILED")
    sys.exit(0 if _ok else 1)
