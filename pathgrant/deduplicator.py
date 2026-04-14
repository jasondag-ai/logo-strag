"""
deduplicator.py

Before any grant record is written to the database, check it
against grants_verified.json and grants_unverified.json to
make sure we are not creating a duplicate of a program that
is already on file (RULE 7).

Dedupe keys:
    1. grant_id exact match
    2. url      exact match

Also provides generate_grant_id() to turn an arbitrary program
name into a stable slug.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# Default paths relative to the pathgrant/ package.
_PATHGRANT_ROOT = Path(__file__).resolve().parent
DEFAULT_VERIFIED_PATH = _PATHGRANT_ROOT / "data" / "grants_verified.json"
DEFAULT_UNVERIFIED_PATH = _PATHGRANT_ROOT / "data" / "grants_unverified.json"

MAX_GRANT_ID_LENGTH = 60


def generate_grant_id(program_name: str) -> str:
    """Convert a program name into a stable slug.

    Rules:
        - lowercase
        - runs of non-alphanumeric characters collapse to a single underscore
        - max 60 characters
        - when truncation is required, break on the last complete word
          boundary (underscore) before the limit so the slug never cuts a
          word in half
        - no leading or trailing underscores
    """
    if not isinstance(program_name, str):
        raise TypeError("program_name must be a string")

    lowered = program_name.lower()
    # Replace every run of non-[a-z0-9] characters with a single space, then
    # collapse that into underscores. Doing it in two passes keeps the regex
    # trivial and makes the intent obvious.
    spaced = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
    slug = re.sub(r"\s+", "_", spaced)

    if len(slug) <= MAX_GRANT_ID_LENGTH:
        return slug.strip("_")

    # Truncation required. Start with the naive cut, then back up to the
    # last underscore inside that window so we never split mid-word. If the
    # first 60 chars contain no usable underscore (single very long word),
    # fall back to the hard cut.
    truncated = slug[:MAX_GRANT_ID_LENGTH]
    last_underscore = truncated.rfind("_")
    if last_underscore > 0:
        truncated = truncated[:last_underscore]
    return truncated.strip("_")


def _load_records(path: Path) -> list[dict[str, Any]]:
    """Read a JSON array of grant records. Empty file or '[]' returns []."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array of records")
    return data


def check_duplicate(
    record: dict[str, Any],
    verified_path: Path | str = DEFAULT_VERIFIED_PATH,
    unverified_path: Path | str = DEFAULT_UNVERIFIED_PATH,
) -> dict[str, Any]:
    """Check whether `record` duplicates an existing grant in either file.

    Two outcomes:

    - grant_id exact match -> hard duplicate, the caller must not insert:
        {"duplicate": True,  "matched_on": "grant_id",
         "existing_record": <grant_id of the match>}

    - url exact match with a DIFFERENT grant_id -> soft collision, the
      caller SHOULD still insert the record but must also surface the
      collision via validation_warnings. Shared source URLs are common
      for government announcements that cover several distinct programs
      on the same page, so a url match on its own is not enough evidence
      that the records are the same program.
        {"duplicate": False, "url_collision": True,
         "matched_on": "url",
         "existing_record": <grant_id of the match>}

    - no match:
        {"duplicate": False}
    """
    existing: list[dict[str, Any]] = []
    existing.extend(_load_records(Path(verified_path)))
    existing.extend(_load_records(Path(unverified_path)))

    new_grant_id = record.get("grant_id")
    new_url = record.get("url")

    # Pass 1: grant_id exact match is a hard duplicate. This is the only
    # hard block; everything else proceeds to insert.
    if new_grant_id:
        for candidate in existing:
            if candidate.get("grant_id") == new_grant_id:
                return {
                    "duplicate": True,
                    "matched_on": "grant_id",
                    "existing_record": candidate.get("grant_id"),
                }

    # Pass 2: url exact match with a different grant_id is a collision,
    # not a duplicate. (url match with the same grant_id would already
    # have been caught above in pass 1.)
    if new_url:
        for candidate in existing:
            if candidate.get("url") == new_url:
                return {
                    "duplicate": False,
                    "url_collision": True,
                    "matched_on": "url",
                    "existing_record": candidate.get("grant_id"),
                }

    return {"duplicate": False}


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

def _run_tests() -> None:
    import tempfile

    record_a = {
        "grant_id": "sport_canada_ssdic_stream_two",
        "program_name": "SSDIC Stream Two",
        "url": "https://www.canada.ca/en/canadian-heritage/services/funding/sport-support.html",
    }
    record_a_dup_id = {
        "grant_id": "sport_canada_ssdic_stream_two",   # same id
        "program_name": "Different program",
        "url": "https://example.org/different-url",
    }
    record_a_dup_url = {
        "grant_id": "totally_different_id",
        "program_name": "Different program",
        "url": record_a["url"],                         # same url
    }
    record_b = {
        "grant_id": "ufa_rural_communities",
        "program_name": "UFA Rural Communities Grant",
        "url": "https://ufafoundation.com/acf-programs-rc-grants/",
    }

    results: list[tuple[str, bool]] = []

    with tempfile.TemporaryDirectory() as tmp:
        verified = Path(tmp) / "grants_verified.json"
        unverified = Path(tmp) / "grants_unverified.json"
        verified.write_text("[]", encoding="utf-8")
        unverified.write_text("[]", encoding="utf-8")

        # ---- Test 1: insert, then same grant_id again ----
        r1a = check_duplicate(record_a, verified, unverified)
        assert r1a == {"duplicate": False}, r1a
        verified.write_text(json.dumps([record_a]), encoding="utf-8")
        r1b = check_duplicate(record_a_dup_id, verified, unverified)
        ok_1 = (
            r1b.get("duplicate") is True
            and r1b.get("matched_on") == "grant_id"
            and r1b.get("existing_record") == record_a["grant_id"]
        )
        results.append(("Test 1: duplicate grant_id", ok_1))
        print(f"[{'PASS' if ok_1 else 'FAIL'}] Test 1: duplicate grant_id")
        print(f"    first insert : {r1a}")
        print(f"    second insert: {r1b}")
        print()

        # ---- Test 2: same url, different grant_id => url_collision ----
        # Under the soft-collision policy this is NOT a hard duplicate.
        # check_duplicate must return duplicate=False with url_collision=True
        # so the caller (ingest.py) can add a url_collision warning to the
        # record's validation_warnings and proceed with the insert.
        r2 = check_duplicate(record_a_dup_url, verified, unverified)
        ok_2 = (
            r2.get("duplicate") is False
            and r2.get("url_collision") is True
            and r2.get("matched_on") == "url"
            and r2.get("existing_record") == record_a["grant_id"]
        )
        results.append(("Test 2: url collision, new grant_id", ok_2))
        print(f"[{'PASS' if ok_2 else 'FAIL'}] Test 2: url collision, new grant_id")
        print(f"    result: {r2}")
        print()

        # ---- Test 3: two genuinely different records both pass ----
        verified.write_text("[]", encoding="utf-8")  # reset
        r3a = check_duplicate(record_a, verified, unverified)
        verified.write_text(json.dumps([record_a]), encoding="utf-8")
        r3b = check_duplicate(record_b, verified, unverified)
        ok_3 = r3a == {"duplicate": False} and r3b == {"duplicate": False}
        results.append(("Test 3: two distinct records", ok_3))
        print(f"[{'PASS' if ok_3 else 'FAIL'}] Test 3: two distinct records")
        print(f"    record_a: {r3a}")
        print(f"    record_b: {r3b}")
        print()

    # ---- Test 4: grant_id generator ----
    # Short names (<= 60 chars) never hit the truncation path.
    # Long names (> 60 chars) must break on a word boundary.
    cases_4 = [
        # Short-name regression cases
        (
            "Sport Canada \u2013 Sport Support Program (SSP)",
            "sport_canada_sport_support_program_ssp",
        ),
        (
            "RBC Foundation: Indigenous Youth Leadership Program!",
            "rbc_foundation_indigenous_youth_leadership_program",
        ),
        (
            "TD Ready Commitment / Better Health \u2014 Youth Wellness 2026",
            "td_ready_commitment_better_health_youth_wellness_2026",
        ),
        # Long-name truncation cases -- must break on last underscore <= 60
        (
            "Sport for Social Development in Indigenous Communities \u2014 Stream Two",
            "sport_for_social_development_in_indigenous_communities",
        ),
        (
            "Canada Summer Jobs Student Work Experience Employment Opportunity Initiative Program",
            "canada_summer_jobs_student_work_experience_employment",
        ),
        (
            "Government of Saskatchewan Sport Development Fund for Rural Youth Communities",
            "government_of_saskatchewan_sport_development_fund_for_rural",
        ),
    ]
    test_4_ok = True
    print("[ ] Test 4: grant_id generator")
    for raw, expected in cases_4:
        got = generate_grant_id(raw)
        case_ok = got == expected
        test_4_ok = test_4_ok and case_ok
        marker = "PASS" if case_ok else "FAIL"
        print(f"    [{marker}] {raw!r}")
        print(f"          expected: {expected!r}")
        print(f"          got     : {got!r}")
    results.append(("Test 4: grant_id generator", test_4_ok))
    print()

    all_passed = all(ok for _, ok in results)
    print("=" * 60)
    print("ALL TESTS PASSED" if all_passed else "ONE OR MORE TESTS FAILED")


if __name__ == "__main__":
    _run_tests()
