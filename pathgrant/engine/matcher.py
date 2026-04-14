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
    parser.add_argument("--top", type=int, default=None)
    args = parser.parse_args()

    client = load_client_profile(args.client)
    grants = load_grants(args.grants)
    ranked = match(client, grants)
    if args.top is not None:
        ranked = ranked[: args.top]

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
    print(
        f"Showing {'top ' + str(args.top) if args.top else 'all'} "
        f"of {len(grants)} ranked by score:"
    )
    print()

    for i, r in enumerate(ranked, 1):
        print(_format_match(i, r))
        print()


if __name__ == "__main__":
    _cli()
