"""
pathgrant/engine/scorer.py

Score one grant against one client profile. Returns a structured score
breakdown (points per signal, points per penalty, hard eliminators) so
matcher can sort grants and print reviewable data rather than narrative.
"""

from __future__ import annotations

from typing import Any


# ----- positive signal weights -----

PROVINCE_GATE_POINTS = 20            # passing the province gate
SECTOR_MATCH_POINTS = 5              # per overlapping sector
INDIGENOUS_ALIGNMENT_POINTS = 25     # Indigenous-led client x Indigenous grant
AMOUNT_LARGE_POINTS = 15             # amount_max >= 100K
AMOUNT_MEDIUM_POINTS = 10            # 25K <= amount_max < 100K
AMOUNT_SMALL_POINTS = 5              # amount_max < 25K
AMOUNT_UNKNOWN_POINTS = 5            # amount_max not numeric
STACKABLE_POINTS = 5                 # stackable == True
TIME_SENSITIVE_POINTS = 15           # within the 60-day deadline window

# ----- penalty weights (negative) -----

SPORTS_TEAM_EXCLUSION_PENALTY = -30           # "sports team" excluded + client is sport org
CAPITAL_ONLY_VS_PRE_LAUNCH_PENALTY = -25      # capital-only grant + pre-launch operating need
MIN_OPERATING_YEARS_PENALTY = -20             # minimum-operating-years clause + pre-launch
GEOGRAPHIC_RISK_PENALTY = -10                 # operator notes stamped "GEOGRAPHIC RISK"
AMOUNT_UNCONFIRMED_PENALTY = -3
DEADLINE_UNKNOWN_PENALTY = -3
URL_COLLISION_PENALTY = -2


def _normalize_sectors(sectors: Any) -> set[str]:
    if not isinstance(sectors, list):
        return set()
    return {s.lower() for s in sectors if isinstance(s, str)}


def _sector_overlap(client_sectors: set[str], grant_sectors: set[str]) -> int:
    """Count overlapping client sectors. Match is exact or substring either way.

    Each client sector counts at most once toward the overlap, even if it
    matches several grant sectors.
    """
    count = 0
    for c in client_sectors:
        for g in grant_sectors:
            if c == g or c in g or g in c:
                count += 1
                break
    return count


def _is_province_eligible(client: dict[str, Any], grant: dict[str, Any]) -> bool:
    client_province = (client.get("province") or "").upper()
    eligible = grant.get("provinces_eligible") or []
    if not isinstance(eligible, list):
        return False
    eligible_upper = {
        e.upper() for e in eligible if isinstance(e, str)
    }
    return "ALL" in eligible_upper or client_province in eligible_upper


def _is_indigenous_grant(grant: dict[str, Any]) -> bool:
    """Grant sectors or eligibility / program text indicate Indigenous focus."""
    if "indigenous" in _normalize_sectors(grant.get("sectors")):
        return True
    corpus_parts: list[str] = []
    corpus_parts.extend(grant.get("eligibility_criteria") or [])
    corpus_parts.append(grant.get("program_name") or "")
    corpus_parts.append(grant.get("funder") or "")
    corpus = " ".join(str(p) for p in corpus_parts).lower()
    return (
        "indigenous" in corpus
        or "first nation" in corpus
        or "metis" in corpus
        or "m\u00e9tis" in corpus
    )


def _amount_points(grant: dict[str, Any]) -> tuple[int, str]:
    amount_max = grant.get("amount_max")
    if not isinstance(amount_max, (int, float)):
        return AMOUNT_UNKNOWN_POINTS, "amount_unknown"
    if amount_max >= 100_000:
        return AMOUNT_LARGE_POINTS, "amount_large"
    if amount_max >= 25_000:
        return AMOUNT_MEDIUM_POINTS, "amount_medium"
    return AMOUNT_SMALL_POINTS, "amount_small"


def _joined_lower(grant: dict[str, Any], field: str) -> str:
    items = grant.get(field) or []
    if not isinstance(items, list):
        return ""
    return " ".join(str(x) for x in items).lower()


def score_grant(
    client: dict[str, Any],
    grant: dict[str, Any],
) -> dict[str, Any]:
    """Score one grant for one client. Pure function; no IO, no randomness."""
    grant_id = grant.get("grant_id")
    program_name = grant.get("program_name")

    signals: list[dict[str, Any]] = []
    penalties: list[dict[str, Any]] = []
    score = 0
    eliminator: str | None = None

    result_shell = {
        "grant_id": grant_id,
        "program_name": program_name,
        "score": 0,
        "signals": signals,
        "penalties": penalties,
        "eliminator": None,
    }

    # --- Hard eliminators ---

    if not _is_province_eligible(client, grant):
        result_shell["eliminator"] = "province_not_eligible"
        return result_shell

    notes_lc = (grant.get("notes") or "").lower()
    if (
        "does not apply to emerge academy" in notes_lc
        or "not applicable to emerge academy" in notes_lc
    ):
        result_shell["eliminator"] = "operator_flag_not_applicable"
        return result_shell

    # --- Positive signals ---

    score += PROVINCE_GATE_POINTS
    signals.append({"label": "province_eligible", "points": PROVINCE_GATE_POINTS})

    overlap = _sector_overlap(
        _normalize_sectors(client.get("sectors")),
        _normalize_sectors(grant.get("sectors")),
    )
    if overlap:
        pts = overlap * SECTOR_MATCH_POINTS
        score += pts
        signals.append(
            {"label": f"sector_overlap_x{overlap}", "points": pts}
        )

    if client.get("Indigenous_led") and _is_indigenous_grant(grant):
        score += INDIGENOUS_ALIGNMENT_POINTS
        signals.append(
            {
                "label": "indigenous_led_client_x_indigenous_grant",
                "points": INDIGENOUS_ALIGNMENT_POINTS,
            }
        )

    amt_pts, amt_label = _amount_points(grant)
    score += amt_pts
    signals.append({"label": amt_label, "points": amt_pts})

    if grant.get("stackable") is True:
        score += STACKABLE_POINTS
        signals.append({"label": "stackable", "points": STACKABLE_POINTS})

    if grant.get("time_sensitive") is True:
        score += TIME_SENSITIVE_POINTS
        signals.append(
            {"label": "time_sensitive", "points": TIME_SENSITIVE_POINTS}
        )

    # --- Penalties ---

    exclusions_lc = _joined_lower(grant, "exclusions")
    eligibility_lc = _joined_lower(grant, "eligibility_criteria")

    client_sport = (client.get("sport") or "").lower().strip()
    if client_sport and "sports team" in exclusions_lc:
        score += SPORTS_TEAM_EXCLUSION_PENALTY
        penalties.append(
            {
                "label": "sports_team_exclusion",
                "points": SPORTS_TEAM_EXCLUSION_PENALTY,
            }
        )

    if client.get("stage") == "pre_launch":
        if (
            "operating cost" in exclusions_lc
            or "capital projects only" in exclusions_lc
            or "capital project" in eligibility_lc
            and "must be a capital project" in eligibility_lc
        ):
            score += CAPITAL_ONLY_VS_PRE_LAUNCH_PENALTY
            penalties.append(
                {
                    "label": "capital_only_vs_pre_launch_operating_need",
                    "points": CAPITAL_ONLY_VS_PRE_LAUNCH_PENALTY,
                }
            )

        if (
            "one year of operation" in eligibility_lc
            or "minimum one year" in eligibility_lc
            or "year of operation" in eligibility_lc
        ):
            score += MIN_OPERATING_YEARS_PENALTY
            penalties.append(
                {
                    "label": "min_operating_years_vs_pre_launch",
                    "points": MIN_OPERATING_YEARS_PENALTY,
                }
            )

    if "geographic risk" in notes_lc:
        score += GEOGRAPHIC_RISK_PENALTY
        penalties.append(
            {"label": "geographic_risk_per_notes", "points": GEOGRAPHIC_RISK_PENALTY}
        )

    for w in grant.get("validation_warnings") or []:
        if not isinstance(w, str):
            continue
        if w.startswith("amount_unconfirmed"):
            score += AMOUNT_UNCONFIRMED_PENALTY
            penalties.append(
                {"label": "amount_unconfirmed", "points": AMOUNT_UNCONFIRMED_PENALTY}
            )
        elif w.startswith("deadline_unknown"):
            score += DEADLINE_UNKNOWN_PENALTY
            penalties.append(
                {"label": "deadline_unknown", "points": DEADLINE_UNKNOWN_PENALTY}
            )
        elif w.startswith("url_collision"):
            score += URL_COLLISION_PENALTY
            penalties.append(
                {"label": "url_collision", "points": URL_COLLISION_PENALTY}
            )

    result_shell["score"] = score
    return result_shell
