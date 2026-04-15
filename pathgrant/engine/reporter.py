"""
pathgrant/engine/reporter.py

Generate a client-specific grant report in Markdown.

Internal operator review format per Task A-C session spec:
  - Markdown only, one .md file per report
  - Concise, professional tone ('$500/hr consultant', not government brochure)
  - Strict section order, omit empty sections
  - Tier 1 (>=60), Tier 2 (30-59), Tier 3 (<30, silently dropped)
  - Reads grants_verified.json always; grants_unverified.json only for the
    Research Queue section; grants_expired.json excluded entirely
  - Only the client profile passed in -- never mixes client data

Writes to:
    pathgrant/reports/<client_id>/<timestamp>.md
    pathgrant/reports/<client_id>/latest.md   (overwritten every run)

Usage:
    python3 pathgrant/engine/reporter.py \
        --client pathgrant/clients/emerge_academy_profile.json

    # preview to stdout without touching the filesystem:
    python3 pathgrant/engine/reporter.py \
        --client pathgrant/clients/emerge_academy_profile.json --stdout
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_PATHGRANT_ROOT = Path(__file__).resolve().parent.parent
if str(_PATHGRANT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PATHGRANT_ROOT))

from engine.matcher import (  # noqa: E402
    load_client_profile,
    load_grants,
    match,
)


DEFAULT_VERIFIED_PATH = _PATHGRANT_ROOT / "data" / "grants_verified.json"
DEFAULT_UNVERIFIED_PATH = _PATHGRANT_ROOT / "data" / "grants_unverified.json"
DEFAULT_REPORTS_ROOT = _PATHGRANT_ROOT / "reports"

TIER_1_THRESHOLD = 60
TIER_2_THRESHOLD = 30

# An eligibility risk is any record whose penalty sum is at or below this
# threshold, OR whose hard eliminator fired with operator_flag_not_applicable.
ELIGIBILITY_RISK_PENALTY_THRESHOLD = -30


# ---------------------------------------------------------------------------
# Tier / filter helpers
# ---------------------------------------------------------------------------

def _tier_for_score(score: int) -> int | None:
    """Return 1, 2, or None (Tier 3, silently dropped from the report)."""
    if score >= TIER_1_THRESHOLD:
        return 1
    if score >= TIER_2_THRESHOLD:
        return 2
    return None


def _sum_penalty_points(result: dict[str, Any]) -> int:
    return sum(p.get("points", 0) for p in result.get("penalties", []))


def _filter_tiered(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in results if _tier_for_score(r["score"]) is not None]


def _province_eligible_for_client(client: dict, grant: dict) -> bool:
    client_prov = (client.get("province") or "").upper()
    eligible = grant.get("provinces_eligible") or []
    if not isinstance(eligible, list):
        return False
    upper = {e.upper() for e in eligible if isinstance(e, str)}
    return "ALL" in upper or client_prov in upper


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_amount(grant: dict[str, Any]) -> str:
    amin = grant.get("amount_min")
    amax = grant.get("amount_max")
    if isinstance(amin, (int, float)) and isinstance(amax, (int, float)):
        return f"${amin:,.0f}–${amax:,.0f}"
    if isinstance(amax, (int, float)):
        return f"up to ${amax:,.0f}"
    if isinstance(amin, (int, float)):
        return f"from ${amin:,.0f}"
    return "Amount not stated"


def _first_sentence(text: str, max_len: int = 240) -> str:
    if not text:
        return ""
    first = text.split(". ", 1)[0].rstrip(".").strip()
    if len(first) > max_len:
        first = first[: max_len - 1].rstrip() + "…"
    return first + "."


def _format_grant_block(
    result: dict[str, Any],
    grant_lookup: dict[str, Any],
    *,
    signal_limit: int = 3,
    intelligence: dict[str, Any] | None = None,
) -> str:
    """Tight grant block for the ranked sections.

    When `intelligence` is provided and the current grant is one of the
    covered top-N grants in intelligence.metadata.top_grants_covered, the
    block appends (a) the per-grant structured narrative and (b) the DIY
    starter kit after the program-page line. When `intelligence` is
    provided but the grant is outside top_grants_covered, a single
    italicized line marks that intelligence is only available for the
    top N. When `intelligence` is None, the block is byte-identical to
    the pre-integration output.
    """
    gid = result["grant_id"]
    name = result["program_name"]
    score = result["score"]
    tier = _tier_for_score(score)
    grant = grant_lookup.get(gid, {})
    url = grant.get("url") or ""

    # Header line -- tier tag + name
    tier_tag = f"Tier {tier}" if tier else "-"
    header = f"### {tier_tag} · {name}"

    # Meta line -- score + flags
    meta_bits = [f"**Score {score}**"]
    if grant.get("time_sensitive"):
        meta_bits.append("⏰ time-sensitive")
    if grant.get("is_repayable"):
        meta_bits.append("repayable")
    if grant.get("stackable") is True:
        meta_bits.append("stackable")
    if isinstance(grant.get("intake_close_date"), str):
        meta_bits.append(f"close {grant['intake_close_date']}")
    elif grant.get("intake_type") == "rolling":
        meta_bits.append("rolling intake")
    meta = " · ".join(meta_bits)

    # Amount line
    amount = _format_amount(grant)
    amount_notes = (grant.get("amount_notes") or "").strip()

    lines = [header, meta, ""]
    if amount_notes:
        lines.append(f"{amount}: {amount_notes}")
    else:
        lines.append(amount)
    lines.append("")

    # Top signals (max signal_limit)
    signals = result.get("signals") or []
    if signals:
        lines.append("**Top signals:**")
        for s in signals[:signal_limit]:
            lines.append(f"- {s['points']:+d} {s['label']}")
        lines.append("")

    # Penalties, if any
    penalties = result.get("penalties") or []
    if penalties:
        lines.append("**Penalties applied:**")
        for p in penalties:
            reason = p.get("reason")
            label = p["label"]
            if reason:
                lines.append(f"- {p['points']:+d} {label}: {reason}")
            else:
                lines.append(f"- {p['points']:+d} {label}")
        lines.append("")

    # Scorer warnings (founder_age_unverified etc.)
    scorer_warnings = result.get("warnings") or []
    if scorer_warnings:
        lines.append("**Scorer warnings:**")
        for w in scorer_warnings:
            lines.append(f"- {w}")
        lines.append("")

    # time_sensitive_note, if present
    if grant.get("time_sensitive_note"):
        lines.append(f"**Deadline note:** {grant['time_sensitive_note']}")
        lines.append("")

    # Program page
    if url:
        lines.append(f"[Program page]({url}) · `{gid}`")
    else:
        lines.append(f"`{gid}`")

    # Intelligence narrative + DIY starter kit (gated by top_grants_covered)
    if intelligence is not None:
        covered = (
            (intelligence.get("metadata") or {}).get("top_grants_covered") or []
        )
        if gid in covered:
            per_grant_intel = (
                (intelligence.get("per_grant") or {}).get(gid) or {}
            )
            structured = per_grant_intel.get("structured")
            diy_kit = per_grant_intel.get("diy_starter_kit")
            if structured:
                lines.append("")
                lines.extend(_format_intelligence_narrative(structured))
            if diy_kit:
                lines.append("")
                lines.extend(_format_diy_starter_kit(diy_kit))
        else:
            lines.append("")
            lines.append(
                "_Intelligence analysis available for top "
                f"{len(covered)} grants only._"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Intelligence helpers -- only used when build_report is called with an
# intelligence dict (produced by engine.intelligence.generate_intelligence).
# When intelligence is None, all of these are bypassed and the reporter
# output is byte-identical to the pre-integration behavior.
# ---------------------------------------------------------------------------

_SENTINEL_PREFIX = "<generation failed"


def _is_sentinel_string(s: Any) -> bool:
    """True if a value is a generation-failure sentinel left by intelligence.py.

    Intelligence calls that fail after all retries leave sentinels in place
    of real content so the downstream report still renders. Reporter uses
    this check to substitute an italicized warning instead of the raw
    sentinel text.
    """
    return isinstance(s, str) and s.startswith(_SENTINEL_PREFIX)


def _shift_diy_headers(md: str) -> str:
    """Rewrite ### sub-headers in a DIY starter kit to #### so the block
    nests cleanly under a grant's ### Tier N header.

    The intelligence module writes the 4 locked sub-headers at ### level
    on the assumption that reporter owns the ## wrapper. When reporter
    embeds the DIY kit inside a ### grant block, the kit's ### headers
    would collide in the hierarchy -- this shifts them one level deeper.
    """
    return re.sub(r"^### ", "#### ", md or "", flags=re.MULTILINE)


def _format_intelligence_narrative(structured: dict[str, Any]) -> list[str]:
    """Format the per-grant structured intelligence block as markdown lines.

    Returns a list of lines ready to extend() into a grant block. Sentinel
    content is substituted with a single italicized warning line; partial
    content is rendered where present.
    """
    if not structured:
        return []

    why = structured.get("why_client_qualifies") or ""
    if _is_sentinel_string(why):
        return [
            "---",
            "",
            "**Intelligence analysis:** "
            "_generation failed for this grant. Re-run "
            "`intelligence.py --client <client_id>` to retry._",
        ]

    lines: list[str] = ["---", ""]

    lines.append("**Why this fits:**")
    lines.append("")
    lines.append(why.strip())
    lines.append("")

    positioning = (structured.get("positioning_angle") or "").strip()
    if positioning:
        lines.append("**Positioning angle:**")
        lines.append("")
        lines.append(positioning)
        lines.append("")

    what_not = structured.get("what_not_to_emphasize") or []
    if what_not:
        lines.append("**What NOT to emphasize:**")
        for item in what_not:
            lines.append(f"- {item}")
        lines.append("")

    docs = structured.get("required_documents") or []
    if docs:
        lines.append("**Required documents:**")
        for d in docs:
            lines.append(f"- {d}")
        lines.append("")

    risks = (structured.get("eligibility_risks") or "").strip()
    if risks:
        lines.append("**Eligibility risks:**")
        lines.append("")
        lines.append(risks)
        lines.append("")

    strategy = structured.get("application_strategy") or {}
    if strategy:
        lines.append("**Application strategy:**")
        lines.append("")

        narrative = (strategy.get("narrative_framework") or "").strip()
        if narrative:
            lines.append(f"_Narrative framework:_ {narrative}")
            lines.append("")

        sequencing = (strategy.get("sequencing_and_dependencies") or "").strip()
        if sequencing:
            lines.append(f"_Sequencing & dependencies:_ {sequencing}")
            lines.append("")

        rejection_reasons = strategy.get("common_rejection_reasons") or []
        if rejection_reasons:
            lines.append("_Common rejection reasons:_")
            for r in rejection_reasons:
                lines.append(f"- {r}")
            lines.append("")

        action_list = strategy.get("client_specific_action_list") or []
        if action_list:
            lines.append("_Client action list:_")
            for a in action_list:
                lines.append(f"- {a}")
            lines.append("")

    notes = (structured.get("notes") or "").strip()
    if notes:
        lines.append(f"_Notes:_ {notes}")
        lines.append("")

    return lines


def _format_diy_starter_kit(diy_md: str) -> list[str]:
    """Format the DIY starter kit as markdown lines. Shifts ### -> ####."""
    if not diy_md:
        return []

    if _is_sentinel_string(diy_md):
        return [
            "---",
            "",
            "**DIY starter kit:** _generation failed. "
            "Re-run `intelligence.py` to retry this section._",
        ]

    lines: list[str] = ["---", "", "**DIY starter kit:**", ""]
    lines.append(_shift_diy_headers(diy_md.strip()))
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Em dash stripper -- Directive v2.0 rule 9 enforcement applied to any
# string rendered into the report. Source grant JSON may contain em dashes
# in notes and other free-text fields; this ensures they never leak into
# the generated output. Duplicates intelligence.py's strip_em_dashes to
# keep reporter self-contained.
# ---------------------------------------------------------------------------

_EM_DASH = "\u2014"
_EM_DASH_SPACED_RE = re.compile(rf" {_EM_DASH} ")
_EM_DASH_EOL_RE = re.compile(rf"\s*{_EM_DASH}(?=\n|$)")


def _strip_em_dashes(text: str) -> str:
    """Replace em dashes with context-appropriate punctuation.

    Case 1: " \u2014 " (spaced) -> ": "
    Case 2: "\u2014" at end of line or string -> "."
    Case 3: "\u2014" (bare) -> "-"
    """
    text = _EM_DASH_SPACED_RE.sub(": ", text)
    text = _EM_DASH_EOL_RE.sub(".", text)
    text = text.replace(_EM_DASH, "-")
    return text


def _strip_em_dashes_recursive(obj: Any) -> Any:
    """Walk a parsed JSON object and apply _strip_em_dashes to every string.

    Returns a new object; does not mutate the input.
    """
    if isinstance(obj, str):
        return _strip_em_dashes(obj)
    if isinstance(obj, dict):
        return {k: _strip_em_dashes_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_em_dashes_recursive(x) for x in obj]
    return obj


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _section_header(
    client: dict[str, Any],
    generated_at: datetime,
    n_verified: int,
    n_unverified: int,
) -> str:
    ts = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"# PathGrant Report: {client.get('organization_name', '')}\n\n"
        f"**Generated:** {ts}  \n"
        f"**Client ID:** `{client.get('client_id', '')}`  \n"
        f"**Grants source:** {n_verified} verified, {n_unverified} unverified "
        f"(research queue only)\n"
    )


_STALE_DEADLINE_NOTE_RE = re.compile(r"^Deadline\s+\d{4}-\d{2}-\d{2}\s+is\s+")


def _compute_fresh_deadline_note(close_date: str, today: str) -> str | None:
    """Return a freshly-computed 'Deadline X is Y days away' string.

    Uses today's actual date, not any date baked into the grant JSON.
    Returns None if close_date is not a parseable ISO date.
    """
    try:
        close_d = datetime.strptime(close_date, "%Y-%m-%d").date()
        today_d = datetime.strptime(today, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    days = (close_d - today_d).days
    if days < 0:
        return (
            f"Deadline {close_date} passed {-days} days ago "
            f"(current date {today})"
        )
    if days == 0:
        return f"Deadline {close_date} is today (current date {today})"
    return (
        f"Deadline {close_date} is {days} days away "
        f"(current date {today})"
    )


def _section_alerts(
    time_sensitive: list[dict[str, Any]],
    grant_lookup: dict[str, Any],
    cite,
    today: str,
) -> str:
    """Render the Alerts section. Deadline calculations use the supplied
    today value, not whatever stale math is hardcoded into the grant's
    time_sensitive_note field.
    """
    if not time_sensitive:
        return ""
    time_sensitive = sorted(time_sensitive, key=lambda r: -r["score"])
    parts = ["## ⏰ Alerts: time-sensitive deadlines", ""]
    for r in time_sensitive:
        cite(r["grant_id"])
        grant = grant_lookup.get(r["grant_id"], {})
        close = grant.get("intake_close_date") or "deadline TBD"

        # Always compute deadline math fresh from today's date.
        fresh = (
            _compute_fresh_deadline_note(close, today)
            if close != "deadline TBD"
            else None
        )

        # Grant's stored note may contain either:
        #   (a) stale date math (starts with "Deadline <ISO> is ...") -- drop it
        #   (b) genuine context (e.g., "Community-specific deadlines vary...") -- keep it
        raw_note = (grant.get("time_sensitive_note") or "").strip()
        if raw_note and _STALE_DEADLINE_NOTE_RE.match(raw_note):
            raw_note = ""

        if fresh and raw_note:
            note = f"{fresh}. {raw_note}"
        elif fresh:
            note = fresh
        elif raw_note:
            note = raw_note
        else:
            note = "In deadline window."

        parts.append(f"- **{r['program_name']}**, close {close}, score {r['score']}")
        parts.append(f"  _{note}_")
    parts.append("")
    return "\n".join(parts)


def _section_client_snapshot(client: dict[str, Any]) -> str:
    name = client.get("organization_name", "")
    entity_type = (
        client.get("legal_structure")
        or client.get("organization_profit_status")
        or "unspecified"
    )
    province = client.get("province", "")
    city = client.get("city")
    if city:
        province = f"{province} ({city})"
    stage = client.get("stage", "")
    sectors = ", ".join(client.get("sectors") or [])
    applicant = client.get("grant_applicant_entity", "-")
    return (
        f"## Client Snapshot\n\n"
        f"- **Name:** {name}\n"
        f"- **Entity type:** {entity_type}\n"
        f"- **Province:** {province}\n"
        f"- **Stage:** {stage}\n"
        f"- **Sectors:** {sectors}\n"
        f"- **Grant applicant entity:** {applicant}\n"
    )


def _section_ranked_bucket(
    bucket: list[dict[str, Any]],
    grant_lookup: dict[str, Any],
    cite,
    *,
    header: str,
    limit: int | None,
    intelligence: dict[str, Any] | None = None,
) -> str:
    tiered = _filter_tiered(bucket)
    if limit is not None:
        tiered = tiered[:limit]
    if not tiered:
        return ""
    parts = [f"## {header}", ""]
    for r in tiered:
        cite(r["grant_id"])
        parts.append(
            _format_grant_block(r, grant_lookup, intelligence=intelligence)
        )
        parts.append("")
    return "\n".join(parts)


def _section_eligibility_risks(
    scored: list[dict[str, Any]],
    grant_lookup: dict[str, Any],
    cite,
) -> str:
    risks: list[tuple[dict, str]] = []
    for r in scored:
        if r.get("eliminator") == "operator_flag_not_applicable":
            risks.append((r, "operator flag: not applicable per record notes"))
            continue
        penalty_sum = _sum_penalty_points(r)
        if penalty_sum <= ELIGIBILITY_RISK_PENALTY_THRESHOLD:
            labels = "; ".join(
                (p.get("reason") or p["label"]) for p in (r.get("penalties") or [])
            )
            risks.append((r, f"penalty sum {penalty_sum}: {labels}"))
    if not risks:
        return ""
    parts = [
        "## Eligibility Risks",
        "",
        "Records that either tripped an operator-flagged not-applicable notes or "
        "accumulated at least 30 points of penalty. Surface these during client "
        "review so ineligible programs do not end up in the shortlist.",
        "",
    ]
    for r, reason in risks:
        cite(r["grant_id"])
        parts.append(
            f"- **{r['program_name']}**, score {r['score']}  "
            f"\n  _{reason}_"
        )
    parts.append("")
    return "\n".join(parts)


def _section_research_queue(
    client: dict[str, Any],
    unverified: list[dict[str, Any]],
    cite,
) -> str:
    # Province-eligible, excluding scoring_note records (those go to Advisory).
    relevant = [
        g for g in unverified
        if _province_eligible_for_client(client, g)
        and g.get("record_type") != "scoring_note"
    ]
    if not relevant:
        return ""
    parts = [
        "## Research Queue (unverified)",
        "",
        "Records in `grants_unverified.json`. **Do not cite in client "
        "deliverables until the source program page has been manually "
        "confirmed.** Contact the funder to verify current intake, amounts, "
        "and eligibility.",
        "",
    ]
    for g in relevant:
        gid = g["grant_id"]
        cite(gid)
        name = g.get("program_name", "")
        status = g.get("status", "")
        note = _first_sentence(g.get("notes") or "")
        parts.append(f"- **{name}**, status `{status}`, `{gid}`")
        if note:
            parts.append(f"  {note}")
    parts.append("")
    return "\n".join(parts)


def _section_advisory(
    advisory: list[dict[str, Any]],
    grant_lookup: dict[str, Any],
    unverified_lookup: dict[str, Any],
    cite,
) -> str:
    if not advisory:
        return ""
    parts = [
        "## Advisory Notes",
        "",
        "Informational records (`record_type: scoring_note`). Not standalone "
        "funding opportunities; context for other applications.",
        "",
    ]
    for r in advisory:
        gid = r["grant_id"]
        cite(gid)
        grant = grant_lookup.get(gid) or unverified_lookup.get(gid) or {}
        note = _first_sentence(grant.get("notes") or "")
        parts.append(f"- **{r['program_name']}**, `{gid}`")
        if note:
            parts.append(f"  {note}")
    parts.append("")
    return "\n".join(parts)


def _section_sources(
    cited_ids: list[str],
    lookup: dict[str, Any],
) -> str:
    if not cited_ids:
        return "## Sources\n\n_No records cited in this report._\n"
    parts = [
        "## Sources",
        "",
        "| grant_id | status | last_verified | url |",
        "|---|---|---|---|",
    ]
    for gid in cited_ids:
        grant = lookup.get(gid, {})
        url = grant.get("url", "")
        last = grant.get("last_verified", "")
        status = grant.get("status", "")
        parts.append(f"| `{gid}` | {status} | {last} | {url} |")
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Intelligence-driven sections: SR&ED, 90-day plan, Stragentic CTA.
# Only rendered when build_report is called with intelligence=<dict>.
# ---------------------------------------------------------------------------

def _section_sred_assessment(intel_sred: dict[str, Any] | None) -> str:
    """Render SR&ED assessment from intelligence.per_client.sred_assessment.

    Always renders when intelligence is present, per operator spec: the
    'correctly rejected and here's why' reasoning is client value, not
    noise. Sentinel content collapses to an italicized warning.
    """
    if not intel_sred:
        return ""

    reasoning = intel_sred.get("reasoning") or ""
    if _is_sentinel_string(reasoning):
        return (
            "## SR&ED Assessment\n\n"
            "> _Intelligence generation failed for this section. "
            "Re-run `intelligence.py --client <client_id>` to retry._\n"
        )

    likely = intel_sred.get("likely_eligible")
    if likely is True:
        label = "Yes"
    elif likely is False:
        label = "No"
    elif likely == "partial":
        label = "Partial"
    else:
        label = "Unknown"

    lines = ["## SR&ED Assessment", ""]
    lines.append(f"**Likely eligible:** {label}")
    lines.append("")
    lines.append(reasoning.strip())

    activities = intel_sred.get("eligible_activities") or []
    if activities:
        lines.append("")
        lines.append("**Eligible activities:**")
        for a in activities:
            lines.append(f"- {a}")

    blockers = intel_sred.get("blocking_factors") or []
    if blockers:
        lines.append("")
        lines.append("**Blocking factors:**")
        for b in blockers:
            lines.append(f"- {b}")

    interaction = (intel_sred.get("interaction_with_other_grants") or "").strip()
    if interaction:
        lines.append("")
        lines.append(f"**Interaction with other grants:** {interaction}")

    recommended = (intel_sred.get("recommended_action") or "").strip()
    if recommended:
        lines.append("")
        lines.append(f"**Recommended action:** {recommended}")

    lines.append("")
    return "\n".join(lines)


def _section_90_day_plan(plan_md: str | None) -> str:
    """Wrap the freeform 90-day plan markdown with the ## header.

    The intelligence module writes the 5 locked sub-headers (### Weeks 1-4
    through ### Responsible parties) at ### level; reporter owns the ##
    wrapper. Sentinel content collapses to an italicized warning.
    """
    if not plan_md:
        return ""
    if _is_sentinel_string(plan_md):
        return (
            "## 90-Day Action Plan\n\n"
            f"> _{plan_md}_\n"
        )
    return "## 90-Day Action Plan\n\n" + plan_md.strip() + "\n"


# Static hardcoded engagement options -- prices and descriptions are
# Stragentic business terms, NOT LLM-generated. Only the per-client
# narrative (summary_of_delivered, rationale, roi_math, recommended
# option letter) comes from intelligence.per_client.stragentic_cta.
_STRAGENTIC_OPTIONS = {
    "A": {
        "label": "Option A: DIY + Review",
        "price": "$1,500 / grant",
        "description": (
            "Stragentic reviews your self-drafted application before "
            "submission. Structural feedback, eligibility check, final "
            "polish. You own the writing; we catch the mistakes."
        ),
    },
    "B": {
        "label": "Option B: Stragentic Drafts",
        "price": "$3,500 – $5,000 / grant",
        "description": (
            "Stragentic drafts the complete application based on your "
            "program documentation. You approve, sign, submit. "
            "Typical two-week turnaround per grant."
        ),
    },
    "C": {
        "label": "Option C: Full Service",
        "price": "$7,500 + 3% of awarded",
        "description": (
            "Stragentic owns the entire application lifecycle: narrative, "
            "budget, supporting documents, funder communications, "
            "revisions. Fixed fee plus success-based percentage. For "
            "high-stakes applications where execution risk must be "
            "eliminated."
        ),
    },
}


def _section_stragentic_cta(intel_cta: dict[str, Any] | None) -> str:
    """Render the Stragentic engagement CTA. Prices are hardcoded; the
    per-client narrative fields come from intelligence.

    The recommended option is rendered first with a ⭐ marker; the other
    two appear after. Contact line always appears at the end.
    """
    if not intel_cta:
        return ""

    summary = (intel_cta.get("summary_of_delivered") or "").strip()
    if _is_sentinel_string(summary):
        return (
            "## Engagement Options\n\n"
            "> _Intelligence generation failed for this section. "
            "Re-run `intelligence.py --client <client_id>` to retry._\n"
        )

    recommended = intel_cta.get("recommended_option") or "A"
    if recommended not in _STRAGENTIC_OPTIONS:
        recommended = "A"
    rationale = (intel_cta.get("recommendation_rationale") or "").strip()
    roi = (intel_cta.get("roi_math") or "").strip()

    lines = ["## Engagement Options", ""]
    if summary:
        lines.append(summary)
        lines.append("")

    # Recommended option first (⭐), then the other two in A/B/C order.
    ordered_letters = [recommended] + [
        l for l in ("A", "B", "C") if l != recommended
    ]
    for letter in ordered_letters:
        opt = _STRAGENTIC_OPTIONS[letter]
        marker = "⭐ **Recommended**  ·  " if letter == recommended else ""
        lines.append(f"### {marker}{opt['label']}  ·  {opt['price']}")
        lines.append("")
        lines.append(opt["description"])
        lines.append("")

    if rationale:
        lines.append(f"**Why Option {recommended} for this client:** {rationale}")
        lines.append("")
    if roi:
        lines.append(f"**ROI math:** {roi}")
        lines.append("")

    lines.append("**Contact:** jason@stragentic.com")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main report assembly
# ---------------------------------------------------------------------------

def build_report(
    client: dict[str, Any],
    verified: list[dict[str, Any]],
    unverified: list[dict[str, Any]],
    *,
    generated_at: datetime | None = None,
    intelligence: dict[str, Any] | None = None,
) -> tuple[str, list[str]]:
    """Return (markdown_body, cited_grant_ids_in_order).

    When `intelligence` is None, the report is structural-only and
    byte-identical to the pre-integration behavior. When intelligence
    is provided, three new top-level sections are inserted
    (SR&ED Assessment, 90-Day Action Plan, Stragentic CTA) and per-grant
    blocks for grants in intelligence.metadata.top_grants_covered get
    the narrative analysis + DIY starter kit appended; grants outside
    top_grants_covered get a one-line italicized availability note.
    Sources always sits last.
    """
    generated_at = generated_at or datetime.now(timezone.utc)

    # Em dash scrub: strip from every string in grant data (source JSON may
    # contain em dashes in notes/amount_notes/time_sensitive_note/etc).
    # Client and intelligence dicts are also scrubbed defensively, though
    # intelligence.py already strips at generation time. Directive v2.0
    # rule 9 enforcement.
    client = _strip_em_dashes_recursive(client)
    verified = _strip_em_dashes_recursive(verified)
    unverified = _strip_em_dashes_recursive(unverified)
    if intelligence is not None:
        intelligence = _strip_em_dashes_recursive(intelligence)

    scored = match(client, verified)
    grant_lookup = {g["grant_id"]: g for g in verified}
    unverified_lookup = {g["grant_id"]: g for g in unverified}

    # Build cited-id accumulator with insertion-order dedup.
    cited_ids: list[str] = []

    def cite(gid: str) -> None:
        if gid not in cited_ids:
            cited_ids.append(gid)

    # --- Partition scored results by intent ---

    # Time-sensitive alerts: any scored record whose source grant has
    # time_sensitive=True AND which passes the tier filter. Tier 3 records
    # never show in alerts (even if time_sensitive) -- if the client is not
    # eligible or severely penalized, the deadline is moot.
    time_sensitive_alerts = [
        r for r in scored
        if grant_lookup.get(r["grant_id"], {}).get("time_sensitive") is True
        and _tier_for_score(r["score"]) is not None
    ]

    program_grants = [
        r for r in scored
        if r.get("record_type") == "grant"
        and r.get("grant_type") == "program_grant"
        and not r.get("is_repayable")
    ]
    sponsorships = [
        r for r in scored
        if r.get("record_type") == "grant"
        and r.get("grant_type") == "sponsorship"
        and not r.get("is_repayable")
    ]
    research_grants = [
        r for r in scored
        if r.get("record_type") == "grant"
        and r.get("grant_type") == "research_grant"
        and not r.get("is_repayable")
    ]
    tax_credits = [
        r for r in scored
        if r.get("record_type") == "grant"
        and r.get("grant_type") == "tax_credit"
        and not r.get("is_repayable")
    ]
    financing = [
        r for r in scored
        if r.get("is_repayable") is True
    ]
    advisory_notes = [
        r for r in scored
        if r.get("record_type") == "scoring_note"
    ]
    # Advisory notes may live in grants_unverified.json, which is not in
    # `scored` -- scan unverified for scoring_note records too.
    for g in unverified:
        if g.get("record_type") == "scoring_note":
            advisory_notes.append(
                {
                    "grant_id": g["grant_id"],
                    "program_name": g.get("program_name", ""),
                    "score": 0,
                    "signals": [],
                    "penalties": [],
                    "warnings": [],
                    "eliminator": None,
                    "grant_type": g.get("grant_type") or "program_grant",
                    "record_type": "scoring_note",
                    "is_repayable": False,
                }
            )

    # --- Assemble sections in spec order ---
    sections: list[str] = []

    sections.append(
        _section_header(client, generated_at, len(verified), len(unverified))
    )

    today_str = generated_at.strftime("%Y-%m-%d")
    alerts_md = _section_alerts(
        time_sensitive_alerts, grant_lookup, cite, today_str
    )
    if alerts_md:
        sections.append(alerts_md)

    sections.append(_section_client_snapshot(client))

    top_md = _section_ranked_bucket(
        program_grants, grant_lookup, cite,
        header="Top Matches: Program Grants", limit=5,
        intelligence=intelligence,
    )
    if top_md:
        sections.append(top_md)

    spon_md = _section_ranked_bucket(
        sponsorships, grant_lookup, cite,
        header="Sponsorships", limit=3,
        intelligence=intelligence,
    )
    if spon_md:
        sections.append(spon_md)

    res_md = _section_ranked_bucket(
        research_grants, grant_lookup, cite,
        header="Research Grants", limit=3,
        intelligence=intelligence,
    )
    if res_md:
        sections.append(res_md)

    tax_md = _section_ranked_bucket(
        tax_credits, grant_lookup, cite,
        header="Tax Credits", limit=None,
        intelligence=intelligence,
    )
    if tax_md:
        sections.append(tax_md)

    fin_md = _section_ranked_bucket(
        financing, grant_lookup, cite,
        header="Financing (repayable)", limit=None,
        intelligence=intelligence,
    )
    if fin_md:
        sections.append(fin_md)

    # SR&ED Assessment -- inserted after Financing, before Eligibility
    # Risks, per operator spec. Only rendered when intelligence is present.
    if intelligence is not None:
        sred_md = _section_sred_assessment(
            (intelligence.get("per_client") or {}).get("sred_assessment")
        )
        if sred_md:
            sections.append(sred_md)

    risk_md = _section_eligibility_risks(scored, grant_lookup, cite)
    if risk_md:
        sections.append(risk_md)

    queue_md = _section_research_queue(client, unverified, cite)
    if queue_md:
        sections.append(queue_md)

    adv_md = _section_advisory(advisory_notes, grant_lookup, unverified_lookup, cite)
    if adv_md:
        sections.append(adv_md)

    # 90-Day Action Plan and Stragentic CTA -- closing argument sections
    # that synthesize across the top grants, placed near the end of the
    # report. Only rendered when intelligence is present.
    if intelligence is not None:
        plan_md = _section_90_day_plan(
            (intelligence.get("per_client") or {}).get("90_day_action_plan")
        )
        if plan_md:
            sections.append(plan_md)

        cta_md = _section_stragentic_cta(
            (intelligence.get("per_client") or {}).get("stragentic_cta")
        )
        if cta_md:
            sections.append(cta_md)

    # Sources always last. Reference appendix -- sits after CTA so the
    # CTA acts as the closing argument and Sources acts as the citation
    # trail readers can scan to verify every grant claim in the report.
    full_lookup = {**grant_lookup, **unverified_lookup}
    sections.append(_section_sources(cited_ids, full_lookup))

    body = "\n---\n\n".join(s.rstrip() + "\n" for s in sections)
    return body, cited_ids


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _timestamp_for_filename(dt: datetime) -> str:
    return dt.strftime("%Y%m%d-%H%M")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Generate a PathGrant Markdown report")
    parser.add_argument("--client", required=True, type=Path)
    parser.add_argument("--verified-path", type=Path, default=DEFAULT_VERIFIED_PATH)
    parser.add_argument("--unverified-path", type=Path, default=DEFAULT_UNVERIFIED_PATH)
    parser.add_argument("--reports-root", type=Path, default=DEFAULT_REPORTS_ROOT)
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print markdown to stdout only; do not write any files",
    )
    parser.add_argument(
        "--intelligence-path",
        type=Path,
        default=None,
        help=(
            "Path to intelligence.json produced by intelligence.py. When "
            "omitted, reporter falls back to structural-only output."
        ),
    )
    args = parser.parse_args()

    client = load_client_profile(args.client)
    verified = load_grants(args.verified_path)
    unverified = load_grants(args.unverified_path)

    intelligence = None
    if args.intelligence_path is not None:
        if not args.intelligence_path.exists():
            raise SystemExit(
                f"--intelligence-path not found: {args.intelligence_path}"
            )
        intelligence = json.loads(
            args.intelligence_path.read_text(encoding="utf-8")
        )

    generated_at = datetime.now(timezone.utc)
    body, cited = build_report(
        client, verified, unverified,
        generated_at=generated_at,
        intelligence=intelligence,
    )

    if args.stdout:
        print(body)
        return

    client_id = client.get("client_id", "unknown")
    ts = _timestamp_for_filename(generated_at)

    out_dir = args.reports_root / client_id
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamped_path = out_dir / f"{ts}.md"
    latest_path = out_dir / "latest.md"

    timestamped_path.write_text(body, encoding="utf-8")
    latest_path.write_text(body, encoding="utf-8")

    print(f"Wrote {timestamped_path}")
    print(f"Wrote {latest_path}")
    print(f"Cited {len(cited)} records")


if __name__ == "__main__":
    _cli()
