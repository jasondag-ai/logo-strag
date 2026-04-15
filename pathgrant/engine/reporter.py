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
) -> str:
    """Tight grant block for the ranked sections."""
    gid = result["grant_id"]
    name = result["program_name"]
    score = result["score"]
    tier = _tier_for_score(score)
    grant = grant_lookup.get(gid, {})
    url = grant.get("url") or ""

    # Header line -- tier tag + name
    tier_tag = f"Tier {tier}" if tier else "—"
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
        lines.append(f"{amount} — {amount_notes}")
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
                lines.append(f"- {p['points']:+d} {label} — {reason}")
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

    return "\n".join(lines)


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
        f"# PathGrant Report — {client.get('organization_name', '')}\n\n"
        f"**Generated:** {ts}  \n"
        f"**Client ID:** `{client.get('client_id', '')}`  \n"
        f"**Grants source:** {n_verified} verified, {n_unverified} unverified "
        f"(research queue only)\n"
    )


def _section_alerts(
    time_sensitive: list[dict[str, Any]],
    grant_lookup: dict[str, Any],
    cite,
) -> str:
    if not time_sensitive:
        return ""
    time_sensitive = sorted(time_sensitive, key=lambda r: -r["score"])
    parts = ["## ⏰ Alerts — time-sensitive deadlines", ""]
    for r in time_sensitive:
        cite(r["grant_id"])
        grant = grant_lookup.get(r["grant_id"], {})
        close = grant.get("intake_close_date") or "deadline TBD"
        note = grant.get("time_sensitive_note") or "In deadline window."
        parts.append(f"- **{r['program_name']}** — close {close}, score {r['score']}")
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
    applicant = client.get("grant_applicant_entity", "—")
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
) -> str:
    tiered = _filter_tiered(bucket)
    if limit is not None:
        tiered = tiered[:limit]
    if not tiered:
        return ""
    parts = [f"## {header}", ""]
    for r in tiered:
        cite(r["grant_id"])
        parts.append(_format_grant_block(r, grant_lookup))
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
            risks.append((r, "operator flag — not applicable per record notes"))
            continue
        penalty_sum = _sum_penalty_points(r)
        if penalty_sum <= ELIGIBILITY_RISK_PENALTY_THRESHOLD:
            labels = "; ".join(
                (p.get("reason") or p["label"]) for p in (r.get("penalties") or [])
            )
            risks.append((r, f"penalty sum {penalty_sum} — {labels}"))
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
            f"- **{r['program_name']}** — score {r['score']}  "
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
        parts.append(f"- **{name}** — status `{status}`, `{gid}`")
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
        "funding opportunities — context for other applications.",
        "",
    ]
    for r in advisory:
        gid = r["grant_id"]
        cite(gid)
        grant = grant_lookup.get(gid) or unverified_lookup.get(gid) or {}
        note = _first_sentence(grant.get("notes") or "")
        parts.append(f"- **{r['program_name']}** — `{gid}`")
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
# Main report assembly
# ---------------------------------------------------------------------------

def build_report(
    client: dict[str, Any],
    verified: list[dict[str, Any]],
    unverified: list[dict[str, Any]],
    *,
    generated_at: datetime | None = None,
) -> tuple[str, list[str]]:
    """Return (markdown_body, cited_grant_ids_in_order)."""
    generated_at = generated_at or datetime.now(timezone.utc)

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

    alerts_md = _section_alerts(time_sensitive_alerts, grant_lookup, cite)
    if alerts_md:
        sections.append(alerts_md)

    sections.append(_section_client_snapshot(client))

    top_md = _section_ranked_bucket(
        program_grants, grant_lookup, cite,
        header="Top Matches — Program Grants", limit=5,
    )
    if top_md:
        sections.append(top_md)

    spon_md = _section_ranked_bucket(
        sponsorships, grant_lookup, cite,
        header="Sponsorships", limit=3,
    )
    if spon_md:
        sections.append(spon_md)

    res_md = _section_ranked_bucket(
        research_grants, grant_lookup, cite,
        header="Research Grants", limit=3,
    )
    if res_md:
        sections.append(res_md)

    tax_md = _section_ranked_bucket(
        tax_credits, grant_lookup, cite,
        header="Tax Credits", limit=None,
    )
    if tax_md:
        sections.append(tax_md)

    fin_md = _section_ranked_bucket(
        financing, grant_lookup, cite,
        header="Financing (repayable)", limit=None,
    )
    if fin_md:
        sections.append(fin_md)

    risk_md = _section_eligibility_risks(scored, grant_lookup, cite)
    if risk_md:
        sections.append(risk_md)

    queue_md = _section_research_queue(client, unverified, cite)
    if queue_md:
        sections.append(queue_md)

    adv_md = _section_advisory(advisory_notes, grant_lookup, unverified_lookup, cite)
    if adv_md:
        sections.append(adv_md)

    # Sources always last.
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
    args = parser.parse_args()

    client = load_client_profile(args.client)
    verified = load_grants(args.verified_path)
    unverified = load_grants(args.unverified_path)

    generated_at = datetime.now(timezone.utc)
    body, cited = build_report(
        client, verified, unverified, generated_at=generated_at
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
