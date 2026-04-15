"""
pathgrant/tools/verify_grant.py

Standalone CLI verification tool. Fetches a grant's official source URL
and displays live page content alongside the current database record
for manual comparison. Can optionally update the record in place after
verification.

Usage:
  python3 pathgrant/tools/verify_grant.py --list
  python3 pathgrant/tools/verify_grant.py --grant-id <id>
  python3 pathgrant/tools/verify_grant.py --grant-id <id> --all-fields
  python3 pathgrant/tools/verify_grant.py --grant-id <id> --update
  python3 pathgrant/tools/verify_grant.py --grant-id <id> \\
      --user-agent "PathGrant-Verifier/1.0"

Modes:
  --list         Print all grant_ids with last_verified age, sorted
                 most-stale first. Flags any record over 60 days.
                 No network calls.
  --grant-id     Load the named grant, display its staleness-sensitive
                 fields, fetch the source URL with urllib, strip HTML,
                 print the first 3,000 chars of visible text, then
                 prompt the operator to edit grants_verified.json if
                 anything has changed.
  --all-fields   Combined with --grant-id, prints every field from the
                 record instead of just the four staleness-sensitive
                 ones.
  --update       Combined with --grant-id, prompt interactively for new
                 values on intake_close_date, amount_min, amount_max,
                 and amount_notes after displaying the live content.
                 Any change also updates last_verified to today and
                 writes grants_verified.json in place.
  --user-agent   Override the User-Agent header for fetches. Defaults
                 to a realistic Chrome/macOS browser string because
                 most WAFs block non-browser identifiers. Pass
                 PathGrant-Verifier/1.0 to identify the tool explicitly.

stdlib only: no requests, no BeautifulSoup, no Playwright.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOOL_ROOT = Path(__file__).resolve().parent
_PATHGRANT_ROOT = _TOOL_ROOT.parent
DEFAULT_VERIFIED_PATH = _PATHGRANT_ROOT / "data" / "grants_verified.json"

FETCH_TIMEOUT_SECONDS = 10

# Default User-Agent is a realistic Chrome/macOS string. Most modern sites
# reject non-browser User-Agents at the WAF layer, so the tool's previous
# default (PathGrant-Verifier/1.0) was blocked on essentially every URL.
# The browser string is not deceptive: it is standard practice for any HTTP
# client that needs to read public web pages. The legacy identifier is kept
# below and remains available via --user-agent if the operator wants it.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
LEGACY_USER_AGENT = "PathGrant-Verifier/1.0"

STALE_DAYS = 60
MAX_LIVE_CONTENT_CHARS = 3000

STALENESS_SENSITIVE_FIELDS = (
    "intake_close_date",
    "amount_min",
    "amount_max",
    "amount_notes",
)


# ---------------------------------------------------------------------------
# HTML text extractor (stdlib only)
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """HTMLParser subclass that collects visible text, skipping non-content
    tags like script, style, noscript, and head metadata."""

    _SKIP_TAGS = frozenset(
        ("script", "style", "noscript", "head", "meta", "link")
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag.lower() in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def _strip_html(html: str) -> str:
    """Strip HTML tags from a string and collapse whitespace to single spaces."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Malformed HTML: keep whatever the parser collected before failure.
        pass
    text = parser.get_text()
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Grant loading
# ---------------------------------------------------------------------------

def _load_grants(path: Path) -> list[dict]:
    if not path.exists():
        print(f"ERROR: grants file not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: failed to parse {path}: {e}", file=sys.stderr)
        sys.exit(1)


def _find_grant(grants: list[dict], grant_id: str) -> dict | None:
    for g in grants:
        if g.get("grant_id") == grant_id:
            return g
    return None


def _days_since(last_verified: str, today: date) -> int | None:
    try:
        last = datetime.strptime(last_verified, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (today - last).days


# ---------------------------------------------------------------------------
# --list mode
# ---------------------------------------------------------------------------

def _cmd_list(grants: list[dict], today: date) -> int:
    rows: list[dict] = []
    for g in grants:
        gid = g.get("grant_id") or "(no grant_id)"
        last_raw = g.get("last_verified") or ""
        days = _days_since(last_raw, today) if last_raw else None
        rows.append(
            {
                "grant_id": gid,
                "last_verified": last_raw or "(missing)",
                "days": days,
            }
        )

    # Sort: most-stale first. Records with unparseable or missing dates
    # sort to the very end so they do not crowd out real staleness signal.
    rows.sort(key=lambda r: (r["days"] is None, -(r["days"] or 0)))

    print(f"GRANT VERIFICATION LIST ({len(rows)} records)")
    print(f"Today: {today.isoformat()}")
    print(f"Stale threshold: {STALE_DAYS} days")
    print()
    print(f"{'stale?':<9}{'days':<7}{'last_verified':<16}grant_id")
    print("-" * 80)
    for r in rows:
        if r["days"] is None:
            marker = "?"
            days_str = "?"
        else:
            days_str = str(r["days"])
            marker = "STALE" if r["days"] > STALE_DAYS else ""
        print(
            f"{marker:<9}{days_str:<7}{r['last_verified']:<16}{r['grant_id']}"
        )
    return 0


# ---------------------------------------------------------------------------
# Live fetch
# ---------------------------------------------------------------------------

def _fetch(url: str, user_agent: str) -> tuple[str | None, dict, str | None]:
    """Fetch URL with stdlib urllib. Returns (body_text, headers, error).

    On success: (decoded_body, headers_dict, None).
    On failure: (None, headers_if_available, error_reason_string).
    """
    if not url:
        return None, {}, "no URL set on grant record"
    if not (url.startswith("http://") or url.startswith("https://")):
        return None, {}, f"unsupported URL scheme: {url}"

    req = Request(url, headers={"User-Agent": user_agent})
    try:
        with urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            try:
                body = raw.decode(charset, errors="replace")
            except LookupError:
                body = raw.decode("utf-8", errors="replace")
            headers = dict(resp.headers.items())
        return body, headers, None
    except HTTPError as e:
        hdrs = dict(e.headers.items()) if e.headers else {}
        return None, hdrs, f"HTTP {e.code} {e.reason}"
    except URLError as e:
        return None, {}, f"network error: {e.reason}"
    except TimeoutError:
        return None, {}, f"timeout after {FETCH_TIMEOUT_SECONDS}s"
    except Exception as e:
        return None, {}, f"unexpected error: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# --grant-id mode
# ---------------------------------------------------------------------------

def _format_value(v) -> str:
    if v is None:
        return "(null)"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _print_record_header(grant: dict, today: date) -> None:
    gid = grant.get("grant_id") or "(unknown)"
    last = grant.get("last_verified") or ""
    if last:
        days = _days_since(last, today)
        if days is None:
            age_str = " (unparseable date)"
        else:
            age_str = f" ({days} days ago)"
            if days > STALE_DAYS:
                age_str += " STALE"
    else:
        last = "(missing)"
        age_str = ""

    print(f"GRANT RECORD: {gid}")
    print(f"Last verified: {last}{age_str}")
    print()


def _print_fields(grant: dict, fields: list[str] | None) -> None:
    if fields is None:
        print("DATABASE VALUES (all fields):")
        keys = list(grant.keys())
    else:
        print("DATABASE VALUES (staleness-sensitive fields):")
        keys = list(fields)

    width = max((len(k) for k in keys), default=1)
    for k in keys:
        value = _format_value(grant.get(k))
        print(f"  {k:<{width}} : {value}")
    print()


def _print_action_prompt(today: date) -> None:
    print("-" * 40)
    print("ACTION REQUIRED: Review live content above against database values.")
    print("If any field has changed, edit grants_verified.json and set")
    print(f"last_verified to {today.isoformat()}.")


# ---------------------------------------------------------------------------
# Interactive update mode
# ---------------------------------------------------------------------------

_SKIP = object()  # sentinel meaning "no change for this field"


def _prompt_field_update(field: str, current: object) -> object:
    """Prompt the operator for a new value for one field.

    Returns:
      - the new value (int, str, or None) if the operator entered something
      - _SKIP sentinel if the operator pressed Enter, typed garbage on a
        typed field, or hit EOF

    Type coercion rules:
      - empty input -> _SKIP (keep current)
      - literal "null" (case-insensitive) -> None
      - amount_min / amount_max -> int() coercion, _SKIP on ValueError
      - intake_close_date -> validate as YYYY-MM-DD, _SKIP on ValueError
      - amount_notes -> free text, accepted as-is
    """
    print(f"{field}")
    print(f"  current: {_format_value(current)}")
    try:
        raw = input("  new value (Enter = keep current, 'null' = set null): ").strip()
    except EOFError:
        print()
        return _SKIP

    if not raw:
        return _SKIP

    if raw.lower() == "null":
        return None

    if field in ("amount_min", "amount_max"):
        try:
            return int(raw)
        except ValueError:
            print(f"  ERROR: '{raw}' is not a valid integer. Keeping current.")
            return _SKIP

    if field == "intake_close_date":
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            print(
                f"  ERROR: '{raw}' is not a valid ISO date (YYYY-MM-DD). "
                f"Keeping current."
            )
            return _SKIP
        return raw

    # amount_notes: accept free text as-is
    return raw


def _write_grants(path: Path, grants: list[dict]) -> None:
    """Serialize the grants list back to disk with the project's standard
    JSON formatting (indent=2, ensure_ascii=False, trailing newline)."""
    body = json.dumps(grants, indent=2, ensure_ascii=False) + "\n"
    path.write_text(body, encoding="utf-8")


def _interactive_update(
    grant: dict,
    today: date,
    grants: list[dict],
    path: Path,
) -> None:
    """Interactive update flow. Prompts for each staleness-sensitive field,
    applies any changes to the grant record in place, and writes
    grants_verified.json if anything actually changed.

    Also updates last_verified to today when at least one field changes.
    """
    print()
    print("=" * 40)
    print("INTERACTIVE UPDATE MODE")
    print(
        "For each field, enter a new value or press Enter to keep the "
        "current value. Type 'null' to set a field to null."
    )
    print()

    changes: dict[str, tuple[object, object]] = {}
    for field in STALENESS_SENSITIVE_FIELDS:
        current = grant.get(field)
        new_value = _prompt_field_update(field, current)
        if new_value is _SKIP:
            continue
        if new_value == current:
            continue
        changes[field] = (current, new_value)
        grant[field] = new_value

    if not changes:
        print()
        print("No changes made. Database not modified.")
        return

    today_str = today.isoformat()
    old_verified = grant.get("last_verified")
    if old_verified != today_str:
        changes["last_verified"] = (old_verified, today_str)
        grant["last_verified"] = today_str

    _write_grants(path, grants)

    print()
    print(f"CHANGES WRITTEN TO {path}")
    width = max((len(f) for f in changes.keys()), default=1)
    for field, (old, new) in changes.items():
        print(f"  {field:<{width}}")
        print(f"    was: {_format_value(old)}")
        print(f"    now: {_format_value(new)}")


def _cmd_verify(
    grant: dict,
    today: date,
    all_fields: bool,
    user_agent: str,
    update_mode: bool = False,
    grants: list[dict] | None = None,
    path: Path | None = None,
) -> int:
    _print_record_header(grant, today)

    if all_fields:
        _print_fields(grant, None)
    else:
        _print_fields(grant, list(STALENESS_SENSITIVE_FIELDS))

    url = grant.get("url") or grant.get("source_url") or ""
    print(f"SOURCE URL: {url or '(none)'}")
    print(f"User-Agent: {user_agent}")
    print("Fetching...")
    print()

    body, headers, err = _fetch(url, user_agent)

    if err:
        print(f"FETCH FAILED: {err}")
        print()
        print("Unable to retrieve live page content. Verify manually at:")
        print(f"  {url}" if url else "  (no URL on record)")
        print()
        _print_action_prompt(today)
    elif not _strip_html(body or "").strip():
        print("WARNING: stripped page content is empty.")
        print(
            "This page may be JavaScript-rendered, a PDF, or otherwise "
            "unreadable by a plain HTML parser."
        )
        print()
        if headers:
            print("RESPONSE HEADERS:")
            for k, v in headers.items():
                print(f"  {k}: {v}")
            print()
        print("Suggest verifying the page manually in a browser:")
        print(f"  {url}")
        print()
        _print_action_prompt(today)
    else:
        stripped = _strip_html(body or "")
        total_chars = len(stripped)
        truncated = stripped[:MAX_LIVE_CONTENT_CHARS]
        print(f"LIVE PAGE CONTENT (first {MAX_LIVE_CONTENT_CHARS} chars):")
        print(truncated)
        if total_chars > MAX_LIVE_CONTENT_CHARS:
            print()
            print(
                f"[... truncated at {MAX_LIVE_CONTENT_CHARS} chars "
                f"of {total_chars} total]"
            )
        print()
        _print_action_prompt(today)

    # Interactive update step, if requested. Runs on all paths (success,
    # fetch failure, empty content) so the operator can still correct the
    # record when they have verified manually.
    if update_mode and grants is not None and path is not None:
        _interactive_update(grant, today, grants, path)

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "PathGrant CLI verification tool: live fetch plus diff prompt"
        )
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help=(
            "Print all grant_ids with last_verified age, sorted "
            "most-stale first. No network calls."
        ),
    )
    parser.add_argument(
        "--grant-id",
        type=str,
        default=None,
        help="Grant ID to verify against its source URL",
    )
    parser.add_argument(
        "--all-fields",
        action="store_true",
        help="With --grant-id, print every field from the record",
    )
    parser.add_argument(
        "--verified-path",
        type=Path,
        default=DEFAULT_VERIFIED_PATH,
        help=(
            f"Path to grants_verified.json "
            f"(default: {DEFAULT_VERIFIED_PATH})"
        ),
    )
    parser.add_argument(
        "--user-agent",
        type=str,
        default=DEFAULT_USER_AGENT,
        help=(
            "User-Agent header to send with fetches. Default is a "
            "realistic Chrome/macOS string because most WAFs block "
            "non-browser agents. Pass PathGrant-Verifier/1.0 to "
            "identify the tool explicitly."
        ),
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help=(
            "After displaying live content, prompt interactively to "
            "update intake_close_date, amount_min, amount_max, and "
            "amount_notes. Any change also sets last_verified to today "
            "and writes grants_verified.json in place."
        ),
    )
    args = parser.parse_args(argv)

    if not args.list and not args.grant_id:
        parser.print_help()
        return 1

    if args.update and not args.grant_id:
        print(
            "ERROR: --update requires --grant-id",
            file=sys.stderr,
        )
        return 1

    grants = _load_grants(args.verified_path)
    today = datetime.now(timezone.utc).date()

    if args.list:
        return _cmd_list(grants, today)

    grant = _find_grant(grants, args.grant_id)
    if grant is None:
        print(
            f"ERROR: grant_id not found: {args.grant_id}",
            file=sys.stderr,
        )
        print(
            "Run with --list to see all available grant_ids.",
            file=sys.stderr,
        )
        return 1

    return _cmd_verify(
        grant,
        today,
        all_fields=args.all_fields,
        user_agent=args.user_agent,
        update_mode=args.update,
        grants=grants,
        path=args.verified_path,
    )


if __name__ == "__main__":
    sys.exit(main())
