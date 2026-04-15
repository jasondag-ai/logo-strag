#!/usr/bin/env python3
"""
run_pathgrant.py -- PathGrant pipeline orchestrator

Single-command runner. Replaces the manual sequence:
  python3 pathgrant/engine/intelligence.py --client <id> ...
  python3 pathgrant/engine/reporter.py --client <profile_path> \
      --intelligence-path <path> --reports-root <dir>

With:
  python3 run_pathgrant.py --client <id>

Runs: validate -> intelligence.py -> reporter.py -> summary.

stdlib only. Subprocess-based so each stage stays isolated and failures
in one stage do not corrupt another stage's output.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths -- script sits at repo root
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
PATHGRANT_DIR = REPO_ROOT / "pathgrant"
CLIENTS_DIR = PATHGRANT_DIR / "clients"
INTELLIGENCE_DIR = PATHGRANT_DIR / "intelligence"
REPORTS_DIR = PATHGRANT_DIR / "reports"
INTELLIGENCE_SCRIPT = PATHGRANT_DIR / "engine" / "intelligence.py"
REPORTER_SCRIPT = PATHGRANT_DIR / "engine" / "reporter.py"


# ---------------------------------------------------------------------------
# Pricing (USD per million tokens) for claude-sonnet-4-6
# ---------------------------------------------------------------------------

INPUT_PRICE_PER_M = 3.00
OUTPUT_PRICE_PER_M = 15.00
CACHE_READ_PRICE_PER_M = 0.30
CACHE_CREATE_PRICE_PER_M = 3.75


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_client_profile(client_id: str) -> dict:
    """Load and return the client profile. Exit 1 if missing or malformed.

    Project convention: pathgrant/clients/<client_id>_profile.json
    """
    profile_path = CLIENTS_DIR / f"{client_id}_profile.json"
    if not profile_path.exists():
        print(
            f"ERROR: Client profile not found: {profile_path}",
            file=sys.stderr,
        )
        print(
            f"  Expected: pathgrant/clients/{client_id}_profile.json",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        with open(profile_path) as f:
            profile = json.load(f)
    except json.JSONDecodeError as e:
        print(
            f"ERROR: Client profile is not valid JSON: {profile_path}",
            file=sys.stderr,
        )
        print(f"  {e}", file=sys.stderr)
        sys.exit(1)
    return profile


def validate_api_key(dry_run: bool = False) -> str:
    """Return API key or exit 1 if missing. Skipped in dry-run."""
    if dry_run:
        return os.environ.get("ANTHROPIC_API_KEY", "dry-run-no-key-needed")
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        print("  export ANTHROPIC_API_KEY=sk-ant-...", file=sys.stderr)
        sys.exit(1)
    if not key.startswith("sk-ant-"):
        print(
            "WARNING: ANTHROPIC_API_KEY does not look like a valid "
            "Anthropic key.",
            file=sys.stderr,
        )
    return key


# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------

def client_profile_path(client_id: str) -> Path:
    """Full path to the client profile JSON file."""
    return CLIENTS_DIR / f"{client_id}_profile.json"


def make_intelligence_path(client_id: str) -> Path:
    """Expected intelligence output path. Matches intelligence.py default."""
    return INTELLIGENCE_DIR / client_id / "latest.json"


def make_report_paths(
    client_id: str,
    client_output_dir: Path,
    timestamp: str,
) -> tuple[Path, Path]:
    """Return (timestamped_path, latest_path). Creates the parent dir.

    The orchestrator passes a client-specific subdirectory (typically
    reports_root / client_id) so the paths are predictable for the
    summary block. Reporter.py writes the actual files using its own
    timestamp so these paths describe the expected layout, not a guarantee
    that exactly timestamped_path exists.
    """
    client_output_dir.mkdir(parents=True, exist_ok=True)
    timestamped = client_output_dir / f"{timestamp}.md"
    latest = client_output_dir / "latest.md"
    return timestamped, latest


# ---------------------------------------------------------------------------
# Subprocess runners
# ---------------------------------------------------------------------------

def run_intelligence(
    client_id: str,
    top_n: int,
    max_tokens: int,
    model: str,
    temperature: float,
    dry_run: bool,
) -> tuple[int, float]:
    """Run intelligence.py. Returns (returncode, elapsed_seconds)."""
    cmd = [
        sys.executable,
        str(INTELLIGENCE_SCRIPT),
        "--client", client_id,
        "--top-n", str(top_n),
        "--max-tokens", str(max_tokens),
        "--model", model,
        "--temperature", str(temperature),
    ]
    if dry_run:
        cmd.append("--dry-run")

    print(f"[intelligence] Running: {' '.join(cmd)}")
    t0 = time.time()
    result = subprocess.run(cmd, env={**os.environ})
    elapsed = time.time() - t0
    return result.returncode, elapsed


def run_reporter(
    client_id: str,
    intelligence_path: Path,
    reports_root: Path,
) -> tuple[int, float]:
    """Run reporter.py. Returns (returncode, elapsed_seconds).

    Reporter writes its own timestamped .md and latest.md under
    <reports_root>/<client_id>/. The orchestrator passes --reports-root
    as a parent directory and lets reporter own the filename layout.
    """
    cmd = [
        sys.executable,
        str(REPORTER_SCRIPT),
        "--client", str(client_profile_path(client_id)),
        "--intelligence-path", str(intelligence_path),
        "--reports-root", str(reports_root),
    ]
    print(f"[reporter]     Running: {' '.join(cmd)}")
    t0 = time.time()
    result = subprocess.run(cmd, env={**os.environ})
    elapsed = time.time() - t0
    return result.returncode, elapsed


# ---------------------------------------------------------------------------
# Summary extraction
# ---------------------------------------------------------------------------

def load_intelligence_summary(intelligence_path: Path) -> dict:
    """Load the intelligence JSON. Returns {} on any error."""
    if not intelligence_path.exists():
        return {}
    try:
        with open(intelligence_path) as f:
            return json.load(f)
    except Exception:
        return {}


def count_api_failures(intel: dict) -> int:
    """Count failed API calls from metadata.api_failures.

    Returns 0 if the metadata block is missing or malformed.
    """
    meta = intel.get("metadata") or {}
    failures = meta.get("api_failures") or []
    if not isinstance(failures, list):
        return 0
    return len(failures)


def count_sentinels(intel: dict) -> int:
    """Count sentinel string markers in the intelligence body.

    intelligence.py emits '<generation failed: ...>' for structured
    sentinels and '[generation failed: ...]' for freeform sentinels.
    Scanning the string bodies catches any failure that reached the
    sentinel fallback even when metadata.api_failures did not capture it.
    """
    markers = ("<generation failed:", "[generation failed:")
    count = 0

    def _scan(obj: object) -> None:
        nonlocal count
        if isinstance(obj, str):
            if any(m in obj for m in markers):
                count += 1
        elif isinstance(obj, dict):
            for v in obj.values():
                _scan(v)
        elif isinstance(obj, list):
            for item in obj:
                _scan(item)

    _scan(intel.get("per_grant"))
    _scan(intel.get("per_client"))
    return count


def compute_cost(intel: dict) -> float:
    """Sum the actual USD cost from metadata.api_call_log token counts.

    Uses claude-sonnet-4-6 pricing. Returns 0.0 if the log is empty.
    """
    meta = intel.get("metadata") or {}
    log = meta.get("api_call_log") or []
    total = 0.0
    for rec in log:
        itok = rec.get("input_tokens") or 0
        otok = rec.get("output_tokens") or 0
        cread = rec.get("cache_read_input_tokens") or 0
        ccreate = rec.get("cache_creation_input_tokens") or 0
        total += (itok / 1_000_000) * INPUT_PRICE_PER_M
        total += (otok / 1_000_000) * OUTPUT_PRICE_PER_M
        total += (cread / 1_000_000) * CACHE_READ_PRICE_PER_M
        total += (ccreate / 1_000_000) * CACHE_CREATE_PRICE_PER_M
    return total


def extract_top_grant(intel: dict) -> tuple[str, str]:
    """Return (display_name, grant_id) for the highest-ranked grant.

    Uses metadata.top_grants_covered, which is already ordered most-
    relevant first. Display name falls back to the grant_id since
    intelligence.py does not store program names alongside the ids.
    """
    meta = intel.get("metadata") or {}
    top_ids = meta.get("top_grants_covered") or []
    if not top_ids:
        return ("N/A", "N/A")
    top_id = top_ids[0]
    return (top_id, top_id)


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def print_summary(
    client_id: str,
    client_name: str,
    top_n: int,
    intel: dict,
    latest_report_path: Path | None,
    intelligence_path: Path,
    intel_elapsed: float,
    reporter_elapsed: float,
    dry_run: bool,
) -> None:
    """Print a compact run summary block."""
    failure_count = count_api_failures(intel)
    sentinel_count = count_sentinels(intel)
    cost_usd = compute_cost(intel) if intel else 0.0
    top_display, _ = extract_top_grant(intel) if intel else ("N/A", "N/A")

    meta = intel.get("metadata") or {}
    top_ids = meta.get("top_grants_covered") or []
    api_calls_total = meta.get("api_calls_total") or 0

    total_elapsed = intel_elapsed + reporter_elapsed
    sep = "-" * 60

    print()
    print(sep)
    print("  PathGrant Run Summary")
    print(sep)
    print(f"  Client:          {client_name} ({client_id})")
    print(f"  Top-N:           {top_n}")
    print(f"  Grants covered:  {len(top_ids)}")
    print(f"  Top grant:       {top_display}")

    if latest_report_path and latest_report_path.exists():
        print(f"  Report:          {latest_report_path}")
    elif dry_run:
        print("  Report:          (dry run, skipped)")
    else:
        print("  Report:          (not written)")

    if intel:
        print(f"  Intelligence:    {intelligence_path}")
    elif dry_run:
        print("  Intelligence:    (dry run, no file written)")
    else:
        print("  Intelligence:    (not generated)")

    print(f"  API calls:       {api_calls_total}")
    if dry_run:
        print("  Cost:            $0.0000 (dry run, no calls made)")
    else:
        print(f"  Cost:            ${cost_usd:.4f}")

    print(
        f"  Runtime:         {total_elapsed:.1f}s "
        f"(intel {intel_elapsed:.1f}s + reporter {reporter_elapsed:.1f}s)"
    )

    if failure_count:
        print(
            f"  API failures:    {failure_count} "
            f"(review metadata.api_failures in the intelligence JSON)"
        )
    if sentinel_count:
        print(
            f"  Sentinel fills:  {sentinel_count} "
            f"(some fields fell back to sentinel placeholders)"
        )

    print(sep)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_pathgrant.py",
        description=(
            "PathGrant pipeline orchestrator. Validates the client "
            "profile, runs intelligence.py, runs reporter.py, and "
            "prints a run summary."
        ),
    )
    p.add_argument(
        "--client",
        required=True,
        help="Client ID (matches pathgrant/clients/<id>_profile.json)",
    )
    p.add_argument(
        "--top-n",
        type=int,
        default=3,
        help="Top N grants for intelligence generation (default: 3)",
    )
    p.add_argument(
        "--max-tokens",
        type=int,
        default=4000,
        help="Max tokens per API call (default: 4000)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print prompts, make no API calls, write no files. "
            "Reporter is skipped entirely in this mode."
        ),
    )
    p.add_argument(
        "--skip-intelligence",
        "--report-only",
        action="store_true",
        help="Use existing latest.json, skip intelligence regeneration",
    )
    p.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="Anthropic model (default: claude-sonnet-4-6)",
    )
    p.add_argument(
        "--temperature",
        type=float,
        default=0.3,
        help="Sampling temperature (default: 0.3)",
    )
    p.add_argument(
        "--reports-root",
        type=Path,
        default=None,
        help=(
            "Override the default reports root (pathgrant/reports/). "
            "Reporter writes to <reports-root>/<client_id>/."
        ),
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    client_id = args.client
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")

    # --- Validation ---
    profile = validate_client_profile(client_id)
    client_name = (
        profile.get("organization_name")
        or profile.get("name")
        or client_id
    )
    validate_api_key(dry_run=args.dry_run)

    intelligence_path = make_intelligence_path(client_id)
    reports_root = args.reports_root or REPORTS_DIR
    client_output_dir = reports_root / client_id
    timestamped_path, latest_path = make_report_paths(
        client_id, client_output_dir, timestamp
    )

    print(f"[pathgrant]    Client: {client_name} ({client_id})")
    print(f"[pathgrant]    Top-N:  {args.top_n}")
    print(f"[pathgrant]    Model:  {args.model}")
    if args.dry_run:
        print("[pathgrant]    Mode:   DRY RUN")
    elif args.skip_intelligence:
        print(
            "[pathgrant]    Mode:   REPORT ONLY "
            "(skip intelligence regeneration)"
        )

    # --- Intelligence ---
    intel_elapsed = 0.0
    if args.skip_intelligence and not args.dry_run:
        if not intelligence_path.exists():
            print(
                f"ERROR: --skip-intelligence requested but no intelligence "
                f"file found: {intelligence_path}",
                file=sys.stderr,
            )
            return 1
        print(
            f"[intelligence] Skipping -- using existing: {intelligence_path}"
        )
    else:
        rc, intel_elapsed = run_intelligence(
            client_id=client_id,
            top_n=args.top_n,
            max_tokens=args.max_tokens,
            model=args.model,
            temperature=args.temperature,
            dry_run=args.dry_run,
        )
        if rc != 0:
            print(
                f"ERROR: intelligence.py exited with code {rc}",
                file=sys.stderr,
            )
            return rc

    # --- Reporter ---
    reporter_elapsed = 0.0
    if args.dry_run:
        print("[reporter]     Skipping -- dry run mode")
    else:
        rc, reporter_elapsed = run_reporter(
            client_id=client_id,
            intelligence_path=intelligence_path,
            reports_root=reports_root,
        )
        if rc != 0:
            print(
                f"ERROR: reporter.py exited with code {rc}",
                file=sys.stderr,
            )
            return rc

    # --- Summary ---
    intel = (
        load_intelligence_summary(intelligence_path)
        if not args.dry_run
        else {}
    )
    # latest.md is guaranteed to exist after a successful reporter run;
    # the timestamped .md also exists but its exact filename depends on
    # reporter's own clock, not the orchestrator's.
    final_report_path = latest_path if not args.dry_run else None

    print_summary(
        client_id=client_id,
        client_name=client_name,
        top_n=args.top_n,
        intel=intel,
        latest_report_path=final_report_path,
        intelligence_path=intelligence_path,
        intel_elapsed=intel_elapsed,
        reporter_elapsed=reporter_elapsed,
        dry_run=args.dry_run,
    )

    return 0


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

def run_tests() -> None:
    """Inline sanity suite. No live API calls, no subprocess, no network."""
    import tempfile
    import unittest

    class OrchestratorTests(unittest.TestCase):
        def setUp(self):
            self.tmpdir = Path(tempfile.mkdtemp())

        def tearDown(self):
            import shutil
            shutil.rmtree(self.tmpdir, ignore_errors=True)

        # --- Client profile validation ---

        def test_validate_client_profile_missing(self):
            import run_pathgrant as mod
            orig = mod.CLIENTS_DIR
            mod.CLIENTS_DIR = self.tmpdir / "clients"
            try:
                with self.assertRaises(SystemExit) as ctx:
                    mod.validate_client_profile("nonexistent_client")
            finally:
                mod.CLIENTS_DIR = orig
            self.assertEqual(ctx.exception.code, 1)

        def test_validate_client_profile_valid(self):
            clients_dir = self.tmpdir / "clients"
            clients_dir.mkdir(parents=True)
            profile = {"organization_name": "Test Org", "province": "SK"}
            profile_path = clients_dir / "test_client_profile.json"
            profile_path.write_text(json.dumps(profile))

            import run_pathgrant as mod
            orig = mod.CLIENTS_DIR
            mod.CLIENTS_DIR = clients_dir
            try:
                result = mod.validate_client_profile("test_client")
            finally:
                mod.CLIENTS_DIR = orig
            self.assertEqual(result["organization_name"], "Test Org")

        def test_validate_client_profile_malformed_json(self):
            clients_dir = self.tmpdir / "clients"
            clients_dir.mkdir(parents=True)
            (clients_dir / "bad_client_profile.json").write_text(
                "{not valid json"
            )

            import run_pathgrant as mod
            orig = mod.CLIENTS_DIR
            mod.CLIENTS_DIR = clients_dir
            try:
                with self.assertRaises(SystemExit) as ctx:
                    mod.validate_client_profile("bad_client")
            finally:
                mod.CLIENTS_DIR = orig
            self.assertEqual(ctx.exception.code, 1)

        # --- API key validation ---

        def test_validate_api_key_missing(self):
            import run_pathgrant as mod
            old = os.environ.pop("ANTHROPIC_API_KEY", None)
            try:
                with self.assertRaises(SystemExit) as ctx:
                    mod.validate_api_key(dry_run=False)
                self.assertEqual(ctx.exception.code, 1)
            finally:
                if old is not None:
                    os.environ["ANTHROPIC_API_KEY"] = old

        def test_validate_api_key_dry_run_skips(self):
            import run_pathgrant as mod
            old = os.environ.pop("ANTHROPIC_API_KEY", None)
            try:
                key = mod.validate_api_key(dry_run=True)
                self.assertEqual(key, "dry-run-no-key-needed")
            finally:
                if old is not None:
                    os.environ["ANTHROPIC_API_KEY"] = old

        # --- Path helpers ---

        def test_make_report_paths(self):
            import run_pathgrant as mod
            base = self.tmpdir / "out"
            ts, latest = mod.make_report_paths(
                "test_client", base, "20260415-1244"
            )
            self.assertEqual(ts.name, "20260415-1244.md")
            self.assertEqual(latest.name, "latest.md")
            self.assertEqual(ts.parent, base)
            self.assertTrue(base.exists())

        def test_make_intelligence_path(self):
            import run_pathgrant as mod
            path = mod.make_intelligence_path("emerge_academy")
            self.assertTrue(
                str(path).endswith("emerge_academy/latest.json")
            )
            self.assertIn("intelligence", str(path))

        def test_client_profile_path_follows_project_convention(self):
            import run_pathgrant as mod
            path = mod.client_profile_path("emerge_academy")
            self.assertEqual(path.name, "emerge_academy_profile.json")

        # --- API failure counting ---

        def test_count_api_failures_empty(self):
            import run_pathgrant as mod
            self.assertEqual(mod.count_api_failures({}), 0)
            self.assertEqual(mod.count_api_failures({"metadata": {}}), 0)
            self.assertEqual(
                mod.count_api_failures({"metadata": {"api_failures": []}}),
                0,
            )

        def test_count_api_failures_counts_list(self):
            import run_pathgrant as mod
            intel = {
                "metadata": {
                    "api_failures": [
                        {"call": 1, "section": "per_grant_structured"},
                        {"call": 4, "section": "sred_assessment"},
                    ]
                }
            }
            self.assertEqual(mod.count_api_failures(intel), 2)

        # --- Sentinel counting ---

        def test_count_sentinels_clean(self):
            import run_pathgrant as mod
            clean = {
                "per_grant": {
                    "g1": {
                        "structured": {"why_client_qualifies": "real content"}
                    }
                },
                "per_client": {"sred_assessment": {"reasoning": "real"}},
            }
            self.assertEqual(mod.count_sentinels(clean), 0)

        def test_count_sentinels_detects_structured_and_freeform(self):
            import run_pathgrant as mod
            dirty = {
                "per_grant": {
                    "g1": {
                        "structured": {
                            "why_client_qualifies": (
                                "<generation failed: parse error>"
                            )
                        },
                        "diy_starter_kit": (
                            "[generation failed: api error]"
                        ),
                    }
                },
                "per_client": {
                    "sred_assessment": {
                        "reasoning": "<generation failed: timeout>"
                    }
                },
            }
            self.assertEqual(mod.count_sentinels(dirty), 3)

        # --- Cost calculation ---

        def test_compute_cost_matches_pricing(self):
            import run_pathgrant as mod
            # 1M input tokens at $3 + 1M output tokens at $15 = $18.00
            intel = {
                "metadata": {
                    "api_call_log": [
                        {
                            "input_tokens": 1_000_000,
                            "output_tokens": 1_000_000,
                        }
                    ]
                }
            }
            self.assertAlmostEqual(mod.compute_cost(intel), 18.00, places=2)

        def test_compute_cost_zero_on_empty_log(self):
            import run_pathgrant as mod
            self.assertEqual(mod.compute_cost({"metadata": {}}), 0.0)
            self.assertEqual(mod.compute_cost({}), 0.0)

        # --- Top grant extraction ---

        def test_extract_top_grant_returns_first_covered(self):
            import run_pathgrant as mod
            intel = {
                "metadata": {
                    "top_grants_covered": [
                        "grant_a",
                        "grant_b",
                        "grant_c",
                    ]
                }
            }
            _, gid = mod.extract_top_grant(intel)
            self.assertEqual(gid, "grant_a")

        def test_extract_top_grant_na_when_empty(self):
            import run_pathgrant as mod
            _, gid = mod.extract_top_grant({"metadata": {}})
            self.assertEqual(gid, "N/A")

    suite = unittest.TestLoader().loadTestsFromTestCase(OrchestratorTests)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--test" in sys.argv:
        run_tests()
    else:
        sys.exit(main())
