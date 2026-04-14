"""
pathgrant/ingest.py

Fixture-mode ingestion pipeline.

The sandbox this project runs in blocks outbound HTTPS, so Priority 1 grant
data arrives as structured input from the operator rather than being fetched
by scraper_base. This module runs a single grant record through the full
validate + dedupe + persist pipeline and logs a scrape_log.json entry with
fetch_method = "fixture".

Usage:
    python3 pathgrant/ingest.py --batch path/to/batch.json

where batch.json is a JSON array of grant records.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


# A record whose intake_close_date is at most this many days out is stamped
# with time_sensitive=True so it can be slotted into time-critical outreach
# without a manual date check.
TIME_SENSITIVE_WINDOW_DAYS = 60


# Make sibling modules (validator.py, deduplicator.py) importable regardless
# of the caller's CWD.
_PATHGRANT_ROOT = Path(__file__).resolve().parent
if str(_PATHGRANT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PATHGRANT_ROOT))

from deduplicator import check_duplicate, generate_grant_id  # noqa: E402
from validator import validate_grant  # noqa: E402


DEFAULT_FIXTURES_DIR = _PATHGRANT_ROOT / "fixtures"
DEFAULT_VERIFIED_PATH = _PATHGRANT_ROOT / "data" / "grants_verified.json"
DEFAULT_UNVERIFIED_PATH = _PATHGRANT_ROOT / "data" / "grants_unverified.json"
DEFAULT_EXPIRED_PATH = _PATHGRANT_ROOT / "data" / "grants_expired.json"
DEFAULT_SCRAPE_LOG_PATH = _PATHGRANT_ROOT / "data" / "scrape_log.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_time_sensitive(
    record: dict[str, Any],
    today: date | None = None,
    window_days: int = TIME_SENSITIVE_WINDOW_DAYS,
) -> tuple[bool, str | None]:
    """Return (flag, note) based on intake_close_date.

    A record is time-sensitive when its intake_close_date is a parseable
    ISO date between today and `window_days` days in the future, inclusive.
    Rolling intakes (null close_date), already-passed close dates, and
    malformed close dates are all treated as not time-sensitive.
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    close_date_str = record.get("intake_close_date")
    if not isinstance(close_date_str, str) or not close_date_str:
        return (False, None)
    try:
        close_date = datetime.strptime(close_date_str, "%Y-%m-%d").date()
    except ValueError:
        return (False, None)
    days_until = (close_date - today).days
    if 0 <= days_until <= window_days:
        note = (
            f"Deadline {close_date_str} is {days_until} days away "
            f"(within {window_days}-day window; current date "
            f"{today.isoformat()})"
        )
        return (True, note)
    return (False, None)


def _load_json_array(path: Path) -> list[dict[str, Any]]:
    """Read a JSON array from disk. Empty file or missing file returns []."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array")
    return data


def _write_json_array(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(records, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _append_json_array(path: Path, record: dict[str, Any]) -> None:
    records = _load_json_array(path)
    records.append(record)
    _write_json_array(path, records)


def ingest_grant(
    record: dict[str, Any],
    *,
    fixtures_dir: Path = DEFAULT_FIXTURES_DIR,
    verified_path: Path = DEFAULT_VERIFIED_PATH,
    unverified_path: Path = DEFAULT_UNVERIFIED_PATH,
    expired_path: Path = DEFAULT_EXPIRED_PATH,
    scrape_log_path: Path = DEFAULT_SCRAPE_LOG_PATH,
) -> dict[str, Any]:
    """Process one grant record through the full ingestion pipeline.

    Steps:
        1. Generate grant_id if the caller did not supply one.
        2. Persist the raw payload to fixtures/<grant_id>.json.
        3. Dedupe against grants_verified.json, grants_unverified.json,
           and grants_expired.json.
        4. Run validate_grant.
        5. Route the record:
             - duplicate                -> skipped, no write
             - validation fails         -> grants_unverified.json
             - validation passes, status == 'expired' -> grants_expired.json
             - validation passes, else  -> grants_verified.json
        6. Append a scrape_log.json entry with fetch_method='fixture'.
    """
    record = dict(record)  # shallow copy so we do not mutate the caller's dict

    # 1. Ensure grant_id, grant_type, and is_repayable.
    #    - grant_type defaults to "program_grant" so the matcher always has
    #      a section to bucket the record into.
    #    - is_repayable defaults to False so the matcher never quietly
    #      treats a new record as a loan just because the operator forgot
    #      to set the field.
    #    The validator still enforces the controlled vocabulary / type
    #    requirements for both fields.
    grant_id = record.get("grant_id")
    if not grant_id:
        grant_id = generate_grant_id(record.get("program_name") or "")
        record["grant_id"] = grant_id
    if "grant_type" not in record:
        record["grant_type"] = "program_grant"
    if "is_repayable" not in record:
        record["is_repayable"] = False
    if "founder_age_restriction" not in record:
        record["founder_age_restriction"] = None
    if "record_type" not in record:
        record["record_type"] = "grant"

    # 2. Persist fixture payload.
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    fixture_path = fixtures_dir / f"{grant_id}.json"
    fixture_path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # 3. Dedupe check. check_duplicate scans verified + unverified. We scan
    #    the expired file inline since check_duplicate does not know about
    #    it. Under the soft-collision policy, grant_id match in any file is
    #    a hard duplicate; url match with a different grant_id is a soft
    #    collision that gets surfaced via validation_warnings rather than
    #    blocking the insert.
    dedupe_result = check_duplicate(record, verified_path, unverified_path)
    if not dedupe_result.get("duplicate"):
        for candidate in _load_json_array(expired_path):
            if candidate.get("grant_id") == grant_id:
                dedupe_result = {
                    "duplicate": True,
                    "matched_on": "grant_id",
                    "existing_record": candidate.get("grant_id"),
                    "matched_in": "expired",
                }
                break  # hard block; nothing else can change this
            if (
                record.get("url")
                and candidate.get("url") == record.get("url")
                and not dedupe_result.get("url_collision")
            ):
                dedupe_result = {
                    "duplicate": False,
                    "url_collision": True,
                    "matched_on": "url",
                    "existing_record": candidate.get("grant_id"),
                    "matched_in": "expired",
                }
                # keep scanning: a later grant_id match would still block

    # 4. Validate.
    validation = validate_grant(record)

    # 5. Build the record that will be persisted. validation_warnings is a
    #    durable field on every stored record (may be []); validation_errors
    #    is only stamped on records that failed validation. The raw fixture
    #    on disk is left alone so fixtures/*.json remains a faithful snapshot
    #    of the operator's input payload.
    _ACTIVE = "active"
    _EXPIRED = "expired"

    persisted = dict(record)
    persisted["validation_warnings"] = list(validation["warnings"])

    # Surface url_collision as a durable warning so downstream consumers of
    # the database files know two programs share a source URL and need a
    # manual check. The warning is appended, not replacing existing warnings.
    if dedupe_result.get("url_collision"):
        persisted["validation_warnings"].append(
            "url_collision - multiple programs share this source URL, "
            "verify these are distinct programs"
        )

    # Stamp time_sensitive / time_sensitive_note for grants whose close date
    # is inside the 60-day window. Records that do not qualify are not
    # stamped at all so the schema stays lean for the common case. When the
    # operator supplied a non-empty time_sensitive_note in the input payload
    # we preserve it -- the operator's domain-specific note is richer than
    # the auto-generated countdown string.
    ts_flag, ts_note = _compute_time_sensitive(record)
    if ts_flag:
        persisted["time_sensitive"] = True
        if not persisted.get("time_sensitive_note"):
            persisted["time_sensitive_note"] = ts_note

    destination: str | None
    if dedupe_result.get("duplicate"):
        destination = None
        outcome = "skipped_duplicate"
    elif not validation["valid"]:
        persisted["validation_errors"] = validation["errors"]
        _append_json_array(unverified_path, persisted)
        destination = "grants_unverified.json"
        outcome = "inserted"
    elif record.get("status") == _EXPIRED:
        _append_json_array(expired_path, persisted)
        destination = "grants_expired.json"
        outcome = "inserted"
    elif record.get("status") == _ACTIVE:
        _append_json_array(verified_path, persisted)
        destination = "grants_verified.json"
        outcome = "inserted"
    else:
        _append_json_array(unverified_path, persisted)
        destination = "grants_unverified.json"
        outcome = "inserted_status_requires_verification"

    # 6. Audit log entry (fixture-mode schema).
    log_entry = {
        "source_url": record.get("source_url"),
        "fetch_method": "fixture",
        "timestamp": _utc_now_iso(),
        "success": True,
        "grant_id": grant_id,
    }
    if dedupe_result.get("url_collision"):
        log_entry["url_collision"] = True
        log_entry["url_collision_with"] = dedupe_result.get("existing_record")
    _append_json_array(scrape_log_path, log_entry)

    try:
        rel_fixture = str(fixture_path.relative_to(_PATHGRANT_ROOT.parent))
    except ValueError:
        rel_fixture = str(fixture_path)

    return {
        "grant_id": grant_id,
        "program_name": record.get("program_name"),
        "destination": destination,
        "outcome": outcome,
        "valid": validation["valid"],
        "errors": validation["errors"],
        "warnings": validation["warnings"],
        "duplicate": dedupe_result,
        "fixture_path": rel_fixture,
        "time_sensitive": persisted.get("time_sensitive", False),
        "time_sensitive_note": persisted.get("time_sensitive_note"),
    }


def _format_report(result: dict[str, Any], index: int) -> str:
    lines = [
        f"--- Record {index} ---",
        f"  program_name    : {result['program_name']}",
        f"  grant_id        : {result['grant_id']}",
        f"  validation      : {'PASS' if result['valid'] else 'FAIL'}",
        f"    errors        : {json.dumps(result['errors'])}",
        f"    warnings      : {json.dumps(result['warnings'])}",
        f"  duplicate check : {json.dumps(result['duplicate'])}",
        f"  destination     : {result['destination']}",
        f"  outcome         : {result['outcome']}",
        f"  fixture         : {result['fixture_path']}",
    ]
    if result.get("time_sensitive"):
        lines.append(f"  time_sensitive  : True")
        lines.append(f"    note          : {result.get('time_sensitive_note')}")
    return "\n".join(lines)


def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Fixture-mode grant ingest pipeline"
    )
    parser.add_argument("--batch", required=True, type=Path, help="JSON array of records")
    args = parser.parse_args()

    records = json.loads(args.batch.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise SystemExit("batch file must contain a JSON list of records")

    for i, record in enumerate(records, 1):
        result = ingest_grant(record)
        print(_format_report(result, i))
        print()


if __name__ == "__main__":
    _cli()
