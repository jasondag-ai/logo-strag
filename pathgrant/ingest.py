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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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

    # 1. Ensure grant_id.
    grant_id = record.get("grant_id")
    if not grant_id:
        grant_id = generate_grant_id(record.get("program_name") or "")
        record["grant_id"] = grant_id

    # 2. Persist fixture payload.
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    fixture_path = fixtures_dir / f"{grant_id}.json"
    fixture_path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # 3. Dedupe check -- all three destination files.
    dedupe_result = check_duplicate(record, verified_path, unverified_path)
    if not dedupe_result.get("duplicate"):
        for candidate in _load_json_array(expired_path):
            matched_on: str | None = None
            if candidate.get("grant_id") == grant_id:
                matched_on = "grant_id"
            elif record.get("url") and candidate.get("url") == record.get("url"):
                matched_on = "url"
            if matched_on:
                dedupe_result = {
                    "duplicate": True,
                    "matched_on": matched_on,
                    "existing_record": candidate.get("grant_id"),
                    "matched_in": "expired",
                }
                break

    # 4. Validate.
    validation = validate_grant(record)

    # 5. Route to destination.
    #
    #    - duplicate                             -> skipped, no write
    #    - validation failure                    -> grants_unverified.json
    #      (with validation_errors / validation_warnings stamped on the copy)
    #    - valid + status == "active"            -> grants_verified.json
    #    - valid + status == "expired"           -> grants_expired.json
    #    - valid + any other status (verify_required, unverified,
    #      url_unverified) -> grants_unverified.json, because a record that
    #      has not been confirmed active does not belong alongside clean,
    #      confirmed grants (RULES 2 + 6).
    _ACTIVE = "active"
    _EXPIRED = "expired"

    destination: str | None
    if dedupe_result.get("duplicate"):
        destination = None
        outcome = "skipped_duplicate"
    elif not validation["valid"]:
        failing_record = dict(record)
        failing_record["validation_errors"] = validation["errors"]
        failing_record["validation_warnings"] = validation["warnings"]
        _append_json_array(unverified_path, failing_record)
        destination = "grants_unverified.json"
        outcome = "inserted"
    elif record.get("status") == _EXPIRED:
        _append_json_array(expired_path, record)
        destination = "grants_expired.json"
        outcome = "inserted"
    elif record.get("status") == _ACTIVE:
        _append_json_array(verified_path, record)
        destination = "grants_verified.json"
        outcome = "inserted"
    else:
        _append_json_array(unverified_path, record)
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
