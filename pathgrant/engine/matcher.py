"""
pathgrant/engine/matcher.py

Rank grants against a client profile using engine.scorer. Returns
structured match data (scores, signals, penalties, eliminators) and
emits the same via a small CLI. No narrative report generation --
that is a separate downstream step.

Usage:
    python3 pathgrant/engine/matcher.py \
        --client pathgrant/clients/emerge_academy_profile.json \
        [--grants pathgrant/data/grants_verified.json] \
        [--top 5]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


_PATHGRANT_ROOT = Path(__file__).resolve().parent.parent
if str(_PATHGRANT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PATHGRANT_ROOT))

from engine.scorer import score_grant  # noqa: E402


DEFAULT_VERIFIED_PATH = _PATHGRANT_ROOT / "data" / "grants_verified.json"


# Display order for grouped matcher output. Any grant_type not listed here
# is treated as a trailing "OTHER" section so no record is silently dropped.
GRANT_TYPE_SECTION_ORDER: tuple[str, ...] = (
    "program_grant",
    "wage_subsidy",
    "capital_grant",
    "sponsorship",
    "research_grant",
)

# Synthetic section key for repayable-instrument records. is_repayable=True
# records bypass grant_type bucketing and land here regardless of their
# nominal grant_type, because a loan is categorically different from a
# non-repayable grant and should never compete in the grant rankings.
FINANCING_SECTION_KEY = "financing"

GRANT_TYPE_SECTION_LABELS: dict[str, str] = {
    "program_grant": "PROGRAM GRANTS (ranked)",
    "wage_subsidy": "WAGE SUBSIDIES (ranked separately)",
    "capital_grant": "CAPITAL GRANTS (ranked separately)",
    "sponsorship": "SPONSORSHIPS (ranked separately)",
    "research_grant": "RESEARCH GRANTS (ranked separately)",
    FINANCING_SECTION_KEY: "FINANCING (repayable, separate from grant rankings)",
}


def load_client_profile(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_grants(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array of grants")
    return data


def match(
    client: dict[str, Any],
    grants: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return grants ranked by score, highest first. Eliminated grants
    (score 0 with an eliminator reason) sort last."""
    scored = [score_grant(client, g) for g in grants]
    # Two-key sort so grants with score 0 and eliminator still appear last.
    scored.sort(
        key=lambda r: (r.get("eliminator") is not None, -r["score"])
    )
    return scored


def group_by_grant_type(
    ranked: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Bucket a ranked list into sections keyed by grant_type.

    is_repayable=True records are pulled out of the grant_type buckets
    entirely and routed to the FINANCING section so loans never compete
    against non-repayable grants in the main rankings. Preserves the
    input ordering within each bucket so the highest-scoring record in
    a section is first. Grant types not listed in
    GRANT_TYPE_SECTION_ORDER fall into a trailing 'other' bucket so
    nothing is silently dropped.
    """
    groups: dict[str, list[dict[str, Any]]] = {
        gt: [] for gt in GRANT_TYPE_SECTION_ORDER
    }
    groups["other"] = []
    groups[FINANCING_SECTION_KEY] = []
    for result in ranked:
        if result.get("is_repayable") is True:
            groups[FINANCING_SECTION_KEY].append(result)
            continue
        gt = result.get("grant_type") or "program_grant"
        if gt in groups:
            groups[gt].append(result)
        else:
            groups["other"].append(result)
    return groups


def _format_match(rank: int, result: dict[str, Any]) -> str:
    lines = [
        f"--- #{rank}: {result['grant_id']} ---",
        f"  program_name : {result['program_name']}",
        f"  score        : {result['score']}",
    ]
    if result.get("eliminator"):
        lines.append(f"  eliminator   : {result['eliminator']}")
    if result.get("signals"):
        lines.append("  signals      :")
        for s in result["signals"]:
            lines.append(f"    + {s['points']:>4d}  {s['label']}")
    if result.get("penalties"):
        lines.append("  penalties    :")
        for p in result["penalties"]:
            label = p["label"]
            if "reason" in p:
                label = f"{p['label']} \u2014 {p['reason']}"
            lines.append(f"    {p['points']:>5d}  {label}")
    return "\n".join(lines)


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Match grants against a client profile")
    parser.add_argument("--client", required=True, type=Path)
    parser.add_argument("--grants", type=Path, default=DEFAULT_VERIFIED_PATH)
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="Limit each grant_type section to the top N within the section",
    )
    args = parser.parse_args()

    client = load_client_profile(args.client)
    grants = load_grants(args.grants)
    ranked = match(client, grants)
    groups = group_by_grant_type(ranked)

    print(
        f"Client: {client.get('organization_name')} "
        f"({client.get('client_id')})"
    )
    print(
        f"Province: {client.get('province')} | "
        f"Stage: {client.get('stage')} | "
        f"Indigenous_led: {client.get('Indigenous_led')} | "
        f"Sport: {client.get('sport')}"
    )
    print(f"Sectors: {client.get('sectors')}")
    print(f"Grants source: {args.grants}  ({len(grants)} records)")
    print("Grouped by grant_type:")
    print()

    section_order = list(GRANT_TYPE_SECTION_ORDER) + ["other", FINANCING_SECTION_KEY]
    for gt in section_order:
        section = groups.get(gt, [])
        if not section:
            continue
        if args.top is not None:
            section = section[: args.top]
        label = GRANT_TYPE_SECTION_LABELS.get(gt, f"OTHER ({gt})")
        print("=" * 70)
        print(f"{label}   [{len(section)} record(s)]")
        print("=" * 70)
        print()
        for i, r in enumerate(section, 1):
            print(_format_match(i, r))
            print()


if __name__ == "__main__":
    _cli()
