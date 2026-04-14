"""
validator.py

Validates PathGrant grant records against the v1 schema.

A record that does not pass validation is routed to
grants_unverified.json rather than grants_verified.json.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


# Fields that must be present and non-null for a record to be valid.
REQUIRED_FIELDS: tuple[str, ...] = (
    "grant_id",
    "program_name",
    "funder",
    "funder_type",
    "url",
    "eligibility_criteria",
    "status",
    "last_verified",
    "source_url",
)

# funder_type controlled vocabulary (RULE 4).
ALLOWED_FUNDER_TYPES: frozenset[str] = frozenset(
    {"federal", "provincial", "corporate", "foundation"}
)

# status controlled vocabulary (RULE 6 + url_unverified from RULE 1).
ALLOWED_STATUSES: frozenset[str] = frozenset(
    {"active", "expired", "verify_required", "unverified", "url_unverified"}
)

# grant_type controlled vocabulary. Field is optional on a record; when set
# it must be one of these values. The matcher groups results by this field.
ALLOWED_GRANT_TYPES: frozenset[str] = frozenset(
    {
        "program_grant",     # funds program operations
        "wage_subsidy",      # subsidizes employee costs
        "capital_grant",     # funds physical assets only
        "sponsorship",       # event or marketing support
        "research_grant",    # R&D and academic collaboration
        "tax_credit",        # federal/provincial tax credit program (SR&ED etc.)
    }
)

# Minimum number of eligibility criteria per RULE 2.
MIN_ELIGIBILITY_CRITERIA = 3

# Substrings (case-insensitive) in amount_notes that indicate the amount is
# not actually confirmed even though the caller may have supplied some text.
# Matched against `amount_notes.lower()`.
AMOUNT_UNCONFIRMED_TRIGGERS: tuple[str, ...] = (
    "tbd",
    "not confirmed",
    "not publicly stated",
    "not disclosed",
    "not stated",
    "contact funder",
    "unknown",
    "varies",
    "case by case",
)

# Single-segment paths that still look like a site root.
_HOMEPAGE_PATH_SEGMENTS: frozenset[str] = frozenset(
    {
        "en",
        "fr",
        "en-ca",
        "fr-ca",
        "en_ca",
        "fr_ca",
        "home",
        "index",
        "index.html",
        "index.htm",
        "default.aspx",
    }
)


def _looks_like_homepage(url: str) -> bool:
    """Heuristic: does the URL point at a site root rather than a program page?

    A URL is treated as a homepage if its path is empty, "/", or a single
    segment that is a common language/home token. Two or more meaningful path
    segments are accepted as a program page.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if not parsed.netloc:
        return False

    segments = [seg for seg in parsed.path.split("/") if seg]

    if len(segments) == 0:
        return True
    if len(segments) == 1 and segments[0].lower() in _HOMEPAGE_PATH_SEGMENTS:
        return True
    return False


def validate_grant(record: dict[str, Any]) -> dict[str, Any]:
    """Validate a single grant record.

    Returns a dict with keys:
        valid    -- bool, True if the record passes hard validation
        errors   -- list of hard-failure messages
        warnings -- list of non-blocking issues

    A homepage-looking URL is a warning only (RULE 1 carve-out): the caller
    may still persist the record but must set status = "url_unverified".
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Required-field presence + non-null check.
    for field in REQUIRED_FIELDS:
        if field not in record:
            errors.append(f"missing required field: {field}")
            continue
        value = record[field]
        if value is None:
            errors.append(f"required field is null: {field}")
            continue
        if isinstance(value, str) and value.strip() == "":
            errors.append(f"required field is empty string: {field}")

    # funder_type must be from controlled vocabulary (RULE 4).
    funder_type = record.get("funder_type")
    if funder_type is not None and funder_type not in ALLOWED_FUNDER_TYPES:
        errors.append(
            f"funder_type '{funder_type}' not in {sorted(ALLOWED_FUNDER_TYPES)}"
        )

    # status must be from controlled vocabulary (RULE 6).
    status = record.get("status")
    if status is not None and status not in ALLOWED_STATUSES:
        errors.append(f"status '{status}' not in {sorted(ALLOWED_STATUSES)}")

    # grant_type is optional but must be from the controlled vocabulary
    # when set. Used by the matcher to group output into sections.
    grant_type = record.get("grant_type")
    if grant_type is not None and grant_type not in ALLOWED_GRANT_TYPES:
        errors.append(
            f"grant_type '{grant_type}' not in {sorted(ALLOWED_GRANT_TYPES)}"
        )

    # is_repayable is optional but must be a boolean when set. Absent is
    # treated as False (i.e. a non-repayable grant) by downstream code.
    is_repayable = record.get("is_repayable")
    if is_repayable is not None and not isinstance(is_repayable, bool):
        errors.append(
            f"is_repayable must be boolean, got {type(is_repayable).__name__}"
        )

    # founder_age_restriction is optional. When set it must be an object
    # with integer min_age / max_age. A non-null value ALWAYS surfaces a
    # warning so the operator knows a manual eligibility check is required:
    # client profiles rarely carry founder age, so this is a soft flag
    # rather than a hard scorer penalty.
    far = record.get("founder_age_restriction")
    if far is not None:
        if not isinstance(far, dict):
            errors.append(
                "founder_age_restriction must be null or an object "
                "with integer min_age and max_age"
            )
        else:
            min_age = far.get("min_age")
            max_age = far.get("max_age")
            if not isinstance(min_age, int) or not isinstance(max_age, int):
                errors.append(
                    "founder_age_restriction.min_age and max_age must both "
                    "be integers"
                )
            else:
                warnings.append(
                    f"founder_age_restriction_set - founder must be "
                    f"{min_age}-{max_age}, manual eligibility check required"
                )

    # eligibility_criteria minimum count (RULE 2).
    criteria = record.get("eligibility_criteria")
    if criteria is not None:
        if not isinstance(criteria, list):
            errors.append("eligibility_criteria must be a list")
        elif len(criteria) < MIN_ELIGIBILITY_CRITERIA:
            errors.append(
                f"eligibility_criteria has {len(criteria)} item(s); "
                f"need at least {MIN_ELIGIBILITY_CRITERIA}"
            )

    # amount_verified must be backed by at least one numeric amount (RULE 3).
    if record.get("amount_verified") is True:
        amt_min = record.get("amount_min")
        amt_max = record.get("amount_max")
        if amt_min is None and amt_max is None:
            errors.append(
                "amount_verified is true but amount_min and amount_max are both null"
            )

    # Data-confidence warning: unverified amounts with ambiguous or missing
    # notes should be surfaced so the operator knows to follow up, but the
    # record is still structurally valid.
    if record.get("amount_verified") is False:
        notes = record.get("amount_notes")
        if notes is None:
            warnings.append(
                "amount_unconfirmed - verify before using in client report"
            )
        else:
            notes_lc = notes.lower()
            if any(trigger in notes_lc for trigger in AMOUNT_UNCONFIRMED_TRIGGERS):
                warnings.append(
                    "amount_unconfirmed - verify before using in client report"
                )

    # Deadline-unknown warning: a non-rolling intake with neither an open
    # nor a close date cannot be slotted into a Tier 1 / Tier 2 decision
    # window without a manual check.
    if record.get("intake_type") != "rolling":
        if (
            record.get("intake_open_date") is None
            and record.get("intake_close_date") is None
        ):
            warnings.append(
                "deadline_unknown - verify before using in Tier 1 or Tier 2 classification"
            )

    # Homepage URL heuristic -- warning, not hard fail (RULE 1).
    url = record.get("url")
    if isinstance(url, str) and url and _looks_like_homepage(url):
        warnings.append(
            f"url '{url}' looks like a homepage, not a program page; "
            "caller should set status = 'url_unverified'"
        )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

def _run_tests() -> None:
    import json

    # Test 1: fully valid record -- should pass, no errors.
    valid_record = {
        "grant_id": "ssdic_stream_two_2026",
        "program_name": "Sport Support Program - SSDIC Stream Two",
        "funder": "Sport Canada",
        "funder_type": "federal",
        "amount_min": None,
        "amount_max": None,
        "amount_notes": "Published amount table lists $50K floor and $250K ceiling",
        "amount_verified": False,
        "intake_type": "biannual",
        "intake_open_date": "2026-10-01",
        "intake_close_date": "2026-12-15",
        "decision_weeks_min": None,
        "decision_weeks_max": None,
        "provinces_eligible": ["ALL"],
        "sectors": ["sport", "Indigenous"],
        "organization_types_eligible": ["NFP", "First Nation"],
        "eligibility_criteria": [
            "Must be an Indigenous not-for-profit organization",
            "Must deliver sport programming to Indigenous participants",
            "Must operate in Canada",
        ],
        "exclusions": ["PTASBs are funded under Stream One, not this stream"],
        "stackable": None,
        "url": "https://www.canada.ca/en/canadian-heritage/services/funding/sport-support.html",
        "contact_email": None,
        "contact_phone": None,
        "status": "active",
        "last_verified": "2026-04-14",
        "source_url": "https://www.canada.ca/en/canadian-heritage/services/funding/sport-support.html",
        "notes": "Test fixture: fully valid",
    }

    # Test 2: missing eligibility_criteria -- should fail.
    missing_criteria_record = {
        "grant_id": "scotiarise_community_2026",
        "program_name": "ScotiaRISE Community Grants",
        "funder": "Scotiabank",
        "funder_type": "corporate",
        "amount_min": None,
        "amount_max": None,
        "amount_notes": None,
        "amount_verified": False,
        "intake_type": "rolling",
        "intake_open_date": None,
        "intake_close_date": None,
        "provinces_eligible": ["ALL"],
        "sectors": ["community"],
        "organization_types_eligible": ["NFP", "charity"],
        # eligibility_criteria intentionally omitted
        "exclusions": ["Sports teams are explicitly excluded"],
        "stackable": None,
        "url": "https://www.scotiabank.com/ca/en/about/responsibility-impact/scotiarise/funding-guidelines-application.html",
        "contact_email": None,
        "contact_phone": None,
        "status": "active",
        "last_verified": "2026-04-14",
        "source_url": "https://www.scotiabank.com/ca/en/about/responsibility-impact/scotiarise/funding-guidelines-application.html",
        "notes": "Test fixture: missing eligibility_criteria",
    }

    # Test 3: amount_verified true but both amounts null -- should fail.
    bad_amount_record = {
        "grant_id": "ufa_rural_communities_2026",
        "program_name": "UFA Rural Communities Grant",
        "funder": "UFA Foundation",
        "funder_type": "foundation",
        "amount_min": None,
        "amount_max": None,
        "amount_notes": "Amount claimed verified but no numbers supplied",
        "amount_verified": True,
        "intake_type": "annual",
        "intake_open_date": None,
        "intake_close_date": None,
        "provinces_eligible": ["AB", "SK", "BC"],
        "sectors": ["rural", "capital_projects"],
        "organization_types_eligible": ["NFP", "charity", "municipal"],
        "eligibility_criteria": [
            "Must be within 200km of a UFA location",
            "Must be a capital project",
            "Must be a registered NFP, charity, or municipality",
        ],
        "exclusions": ["Operating expenses are not eligible"],
        "stackable": None,
        "url": "https://ufafoundation.com/acf-programs-rc-grants/",
        "contact_email": None,
        "contact_phone": None,
        "status": "active",
        "last_verified": "2026-04-14",
        "source_url": "https://ufafoundation.com/acf-programs-rc-grants/",
        "notes": "Test fixture: amount_verified true but no amounts",
    }

    cases = [
        ("Test 1: fully valid record", valid_record, True),
        ("Test 2: missing eligibility_criteria", missing_criteria_record, False),
        ("Test 3: amount_verified true, null amounts", bad_amount_record, False),
    ]

    all_passed = True
    for label, record, expected_valid in cases:
        result = validate_grant(record)
        got_valid = result["valid"]
        ok = got_valid is expected_valid
        if not ok:
            all_passed = False
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        print(f"    expected valid={expected_valid}, got valid={got_valid}")
        print(f"    errors   : {json.dumps(result['errors'])}")
        print(f"    warnings : {json.dumps(result['warnings'])}")
        print()

    print("=" * 60)
    print("ALL TESTS PASSED" if all_passed else "ONE OR MORE TESTS FAILED")


if __name__ == "__main__":
    _run_tests()
