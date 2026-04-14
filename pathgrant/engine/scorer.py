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
NONPROFIT_INELIGIBLE_PENALTY = -80            # NFP client + grant excludes non-profits
FOR_PROFIT_INELIGIBLE_PENALTY = -80           # for-profit client + grant restricts to NFP
AMOUNT_UNCONFIRMED_PENALTY = -3
DEADLINE_UNKNOWN_PENALTY = -3
URL_COLLISION_PENALTY = -2


# ----- NFP / for-profit mismatch lexicons -----

# Phrases (case-insensitive substring match) in grant exclusions that mean
# "non-profits are not eligible; you must be a for-profit entity". The list
# is deliberately redundant -- some phrases are substrings of others -- so
# that each operator-reviewed phrasing is self-documenting in the trigger
# list rather than hiding behind an implicit substring match.
_FOR_PROFIT_EXCLUSION_PHRASES: tuple[str, ...] = (
    "non-profit organizations not eligible",
    "for-profit only",
    "for-profit businesses only",
    "profit-oriented",
    "profit-oriented businesses only",
    "incorporated for-profit",
    "nfp and charitable organizations not eligible",
    "charities not eligible",
    "non-profit not eligible",
)

# Keywords in client legal_structure that identify an NFP or charity client.
_NFP_CLIENT_LEGAL_KEYWORDS: tuple[str, ...] = (
    "nfp",
    "nonprofit",
    "non-profit",
    "charity",
)

# Keywords in grant eligibility text that mention NFP/charity eligibility.
_NFP_ELIGIBILITY_KEYWORDS: tuple[str, ...] = (
    "non-profit",
    "nonprofit",
    "nfp",
    "charity",
)

# Keywords in grant eligibility text that mention for-profit eligibility.
# If any of these appear, the grant is NOT NFP-only, so for_profit_ineligible
# should not fire.
_FOR_PROFIT_ELIGIBILITY_KEYWORDS: tuple[str, ...] = (
    "for-profit",
    "for profit",
    "profit-oriented",
    "sme",
    "small or medium",
)


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


def _client_is_nfp(client: dict[str, Any]) -> bool:
    """Return True if the client profile indicates an NFP or charity."""
    # Canonical structured field takes priority when present.
    if client.get("organization_profit_status") == "non_profit":
        return True
    legal = (client.get("legal_structure") or "").lower()
    if any(kw in legal for kw in _NFP_CLIENT_LEGAL_KEYWORDS):
        return True
    if client.get("nfp_registered") is True:
        return True
    if client.get("registered_charity") is True:
        return True
    return False


def _client_is_for_profit(client: dict[str, Any]) -> bool:
    """Return True if the client profile is explicitly for-profit."""
    # Canonical structured field takes priority; legacy boolean field
    # for_profit=True is still supported for existing test fixtures.
    if client.get("organization_profit_status") == "for_profit":
        return True
    if client.get("for_profit") is True:
        return True
    return False


def _normalized_client_name(client: dict[str, Any]) -> str:
    """Lowercase client name with leading articles stripped, for substring
    matching against operator-stamped 'Not applicable to <name>' notes."""
    name = (client.get("organization_name") or "").lower().strip()
    for prefix in ("the ", "la ", "le ", "les "):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name


def _grant_excludes_nonprofit(grant: dict[str, Any]) -> bool:
    """True if the grant's exclusions text contains a for-profit-only phrase."""
    exclusions_text = _joined_lower(grant, "exclusions")
    return any(
        phrase in exclusions_text for phrase in _FOR_PROFIT_EXCLUSION_PHRASES
    )


def _grant_restricts_to_nonprofit(grant: dict[str, Any]) -> bool:
    """True iff the grant's eligibility mentions NFP/charity AND has no
    for-profit language. Mirrors the 'contain ONLY non-profit/NFP/charity
    references with no for-profit language' spec."""
    eligibility_text = _joined_lower(grant, "eligibility_criteria")
    has_nfp = any(kw in eligibility_text for kw in _NFP_ELIGIBILITY_KEYWORDS)
    has_for_profit = any(
        kw in eligibility_text for kw in _FOR_PROFIT_ELIGIBILITY_KEYWORDS
    )
    return has_nfp and not has_for_profit


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
        "grant_type": grant.get("grant_type") or "program_grant",
        "is_repayable": bool(grant.get("is_repayable")),
        "score": 0,
        "signals": signals,
        "penalties": penalties,
        "eliminator": None,
    }

    # --- Hard eliminators ---

    if not _is_province_eligible(client, grant):
        result_shell["eliminator"] = "province_not_eligible"
        return result_shell

    # Operator flag eliminator. Match against the CURRENT client's name
    # (normalized) rather than a hard-coded 'emerge academy' substring, so
    # a grant whose notes say 'Not applicable to Emerge Academy' does not
    # incorrectly eliminate when scored against Sacral Solutions.
    notes_lc = (grant.get("notes") or "").lower()
    client_name_lc = _normalized_client_name(client)
    if client_name_lc and (
        f"does not apply to {client_name_lc}" in notes_lc
        or f"not applicable to {client_name_lc}" in notes_lc
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

    # NFP client hitting a grant whose exclusions require for-profit status.
    if _client_is_nfp(client) and _grant_excludes_nonprofit(grant):
        score += NONPROFIT_INELIGIBLE_PENALTY
        penalties.append(
            {
                "label": "non_profit_ineligible",
                "points": NONPROFIT_INELIGIBLE_PENALTY,
                "reason": "grant explicitly requires for-profit status",
            }
        )

    # For-profit client hitting a grant whose eligibility is NFP/charity only.
    if _client_is_for_profit(client) and _grant_restricts_to_nonprofit(grant):
        score += FOR_PROFIT_INELIGIBLE_PENALTY
        penalties.append(
            {
                "label": "for_profit_ineligible",
                "points": FOR_PROFIT_INELIGIBLE_PENALTY,
                "reason": "grant requires NFP or charity status",
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


# ---------------------------------------------------------------------------
# Inline tests for the NFP / for-profit mismatch rules
# ---------------------------------------------------------------------------

def _run_tests() -> None:
    import json

    nfp_client = {
        "province": "SK",
        "sectors": ["technology"],
        "legal_structure": "NFP corporation",
        "nfp_registered": True,
        "stage": "operating",
    }

    for_profit_client = {
        "province": "SK",
        "sectors": ["technology"],
        "for_profit": True,
        "stage": "operating",
    }

    for_profit_only_grant = {
        "grant_id": "test_for_profit_only",
        "program_name": "For-Profit Only Test Grant",
        "provinces_eligible": ["SK"],
        "sectors": ["technology"],
        "exclusions": [
            "CRITICAL: Non-profit organizations not eligible for this program",
        ],
        "eligibility_criteria": [
            "Must be incorporated for-profit Canadian company",
            "Must have at least three months of operations",
            "Must serve Canadian markets",
        ],
        "status": "active",
        "amount_max": 50000,
        "amount_verified": True,
        "intake_type": "rolling",
        "stackable": None,
        "validation_warnings": [],
    }

    nfp_only_grant = {
        "grant_id": "test_nfp_only",
        "program_name": "NFP Only Test Grant",
        "provinces_eligible": ["SK"],
        "sectors": ["technology"],
        "exclusions": [
            "Individual pursuits are not supported",
        ],
        "eligibility_criteria": [
            "Must be a registered charity or non-profit organization",
            "Must serve Canadian communities",
            "Must have strong financial management",
        ],
        "status": "active",
        "amount_max": 50000,
        "amount_verified": True,
        "intake_type": "rolling",
        "stackable": None,
        "validation_warnings": [],
    }

    results: list[tuple[str, bool]] = []

    # Test 1: NFP client x for-profit-only grant => non_profit_ineligible fires.
    r1 = score_grant(nfp_client, for_profit_only_grant)
    t1_ok = any(p["label"] == "non_profit_ineligible" for p in r1["penalties"])
    results.append(("Test 1", t1_ok))
    print(f"[{'PASS' if t1_ok else 'FAIL'}] Test 1: NFP client x for-profit-only grant "
          "fires non_profit_ineligible")
    print(f"    penalties: {json.dumps(r1['penalties'])}")
    print()

    # Test 2: NFP client x NFP-only grant => non_profit_ineligible does NOT fire.
    r2 = score_grant(nfp_client, nfp_only_grant)
    t2_ok = not any(p["label"] == "non_profit_ineligible" for p in r2["penalties"])
    results.append(("Test 2", t2_ok))
    print(f"[{'PASS' if t2_ok else 'FAIL'}] Test 2: NFP client x NFP-only grant "
          "does not fire non_profit_ineligible")
    print(f"    penalties: {json.dumps(r2['penalties'])}")
    print()

    # Test 3: for-profit client x NFP-only grant => for_profit_ineligible fires.
    r3 = score_grant(for_profit_client, nfp_only_grant)
    t3_ok = any(p["label"] == "for_profit_ineligible" for p in r3["penalties"])
    results.append(("Test 3", t3_ok))
    print(f"[{'PASS' if t3_ok else 'FAIL'}] Test 3: for-profit client x NFP-only grant "
          "fires for_profit_ineligible")
    print(f"    penalties: {json.dumps(r3['penalties'])}")
    print()

    # Test 4: for-profit client x for-profit-only grant => for_profit_ineligible
    # does NOT fire.
    r4 = score_grant(for_profit_client, for_profit_only_grant)
    t4_ok = not any(p["label"] == "for_profit_ineligible" for p in r4["penalties"])
    results.append(("Test 4", t4_ok))
    print(f"[{'PASS' if t4_ok else 'FAIL'}] Test 4: for-profit client x for-profit-only "
          "grant does not fire for_profit_ineligible")
    print(f"    penalties: {json.dumps(r4['penalties'])}")
    print()

    # Test 5: CDAP-style wording -- 'For-profit businesses only -- NFP not
    # eligible' -- should fire non_profit_ineligible against an NFP client.
    cdap_like_grant = {
        "grant_id": "test_cdap_like",
        "program_name": "CDAP-like Test Grant",
        "provinces_eligible": ["SK"],
        "sectors": ["technology"],
        "exclusions": [
            "CRITICAL: For-profit businesses only -- NFP not eligible",
            "Pre-revenue businesses not eligible",
        ],
        "eligibility_criteria": [
            "Must be incorporated for-profit business",
            "Must have minimum $500,000 in annual revenue",
            "Must adopt new digital technologies",
        ],
        "status": "active",
        "amount_max": 15000,
        "amount_verified": True,
        "intake_type": "rolling",
        "stackable": True,
        "validation_warnings": [],
    }
    r5 = score_grant(nfp_client, cdap_like_grant)
    t5_ok = any(p["label"] == "non_profit_ineligible" for p in r5["penalties"])
    results.append(("Test 5", t5_ok))
    print(f"[{'PASS' if t5_ok else 'FAIL'}] Test 5: NFP client x CDAP-style "
          "'For-profit businesses only' fires non_profit_ineligible")
    print(f"    penalties: {json.dumps(r5['penalties'])}")
    print()

    # Test 6: Futurpreneur-style wording -- 'NFP and charitable organizations
    # not eligible' -- should fire non_profit_ineligible against an NFP client.
    futurpreneur_like_grant = {
        "grant_id": "test_futurpreneur_like",
        "program_name": "Futurpreneur-like Test Grant",
        "provinces_eligible": ["SK"],
        "sectors": ["entrepreneurship"],
        "exclusions": [
            "CRITICAL: Loan not a grant -- repayment required",
            "Entrepreneurs over age 39 not eligible",
            "NFP and charitable organizations not eligible",
        ],
        "eligibility_criteria": [
            "Entrepreneur must be 18 to 39 years of age",
            "Business must be for-profit and early stage",
            "Must work with a Futurpreneur mentor",
        ],
        "status": "active",
        "amount_max": 60000,
        "amount_verified": True,
        "intake_type": "rolling",
        "stackable": True,
        "validation_warnings": [],
    }
    r6 = score_grant(nfp_client, futurpreneur_like_grant)
    t6_ok = any(p["label"] == "non_profit_ineligible" for p in r6["penalties"])
    results.append(("Test 6", t6_ok))
    print(f"[{'PASS' if t6_ok else 'FAIL'}] Test 6: NFP client x Futurpreneur-style "
          "'NFP and charitable organizations not eligible' fires non_profit_ineligible")
    print(f"    penalties: {json.dumps(r6['penalties'])}")
    print()

    # Test 7: tax_credit grant_type must route to the TAX CREDITS section and
    # NEVER into PROGRAM GRANTS. Cross-module test against
    # matcher.group_by_grant_type to prove the end-to-end flow: scorer echoes
    # grant_type into the result, matcher routes on that field.
    tax_credit_grant = {
        "grant_id": "test_sred_like",
        "program_name": "SR&ED-like Test Grant",
        "provinces_eligible": ["SK"],
        "sectors": ["technology", "innovation", "RD"],
        "exclusions": [],
        "eligibility_criteria": [
            "Must be an incorporated Canadian business",
            "Must conduct scientific research or experimental development",
            "Must file a T661 claim form with CRA",
        ],
        "status": "active",
        "amount_max": 500000,
        "amount_verified": True,
        "intake_type": "rolling",
        "stackable": True,
        "validation_warnings": [],
        "grant_type": "tax_credit",
        "is_repayable": False,
    }
    program_grant_comparison = {
        "grant_id": "test_program_grant_comparison",
        "program_name": "Program Grant Test",
        "provinces_eligible": ["SK"],
        "sectors": ["technology"],
        "exclusions": [],
        "eligibility_criteria": [
            "Must be a Canadian organization",
            "Must have a project",
            "Must submit an application",
        ],
        "status": "active",
        "amount_max": 50000,
        "amount_verified": True,
        "intake_type": "rolling",
        "stackable": True,
        "validation_warnings": [],
        "grant_type": "program_grant",
        "is_repayable": False,
    }
    r7_tc = score_grant(for_profit_client, tax_credit_grant)
    r7_pg = score_grant(for_profit_client, program_grant_comparison)
    scorer_ok = (
        r7_tc["grant_type"] == "tax_credit"
        and r7_pg["grant_type"] == "program_grant"
    )
    # Import matcher as a sibling module. When scorer.py runs as __main__,
    # pathgrant/engine/ is sys.path[0], so matcher.py is directly importable
    # without going through the 'engine.matcher' dotted path.
    from matcher import group_by_grant_type  # noqa: E402
    groups = group_by_grant_type([r7_tc, r7_pg])
    routing_ok = (
        any(g["grant_id"] == "test_sred_like" for g in groups["tax_credit"])
        and not any(g["grant_id"] == "test_sred_like" for g in groups["program_grant"])
        and any(g["grant_id"] == "test_program_grant_comparison" for g in groups["program_grant"])
    )
    t7_ok = scorer_ok and routing_ok
    results.append(("Test 7", t7_ok))
    print(f"[{'PASS' if t7_ok else 'FAIL'}] Test 7: tax_credit grant_type routes to "
          "TAX CREDITS bucket, never to PROGRAM GRANTS")
    print(f"    scorer_ok : {scorer_ok} (grant_type fields echoed correctly)")
    print(f"    routing_ok: {routing_ok} (matcher.group_by_grant_type placement)")
    print(
        f"    tax_credit bucket : "
        f"{[g['grant_id'] for g in groups['tax_credit']]}"
    )
    print(
        f"    program_grant bucket : "
        f"{[g['grant_id'] for g in groups['program_grant']]}"
    )
    print()

    all_passed = all(ok for _, ok in results)
    print("=" * 60)
    print("ALL TESTS PASSED" if all_passed else "ONE OR MORE TESTS FAILED")


if __name__ == "__main__":
    _run_tests()
