"""
scraper_base.py

Base HTTP + HTML scraping class for the PathGrant harvester.

Responsibilities:
    - Rate-limited HTTP GETs (2 second minimum interval between requests)
    - Standard browser User-Agent to avoid trivial blocks
    - Retry logic: 2 retries on failure with a 5 second delay between attempts
    - Graceful failure: never crash the caller, log the failure and return None
    - Append every request outcome to pathgrant/data/scrape_log.json so we
      have a complete audit trail (RULE 5)

Subclasses should call self.fetch(url) for every page they need and then use
extract_text() / extract_links() to pull structured data off the returned
BeautifulSoup object.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup


# --- paths ------------------------------------------------------------------

_PATHGRANT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCRAPE_LOG_PATH = _PATHGRANT_ROOT / "data" / "scrape_log.json"


# --- HTTP defaults ----------------------------------------------------------

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_INTERVAL_SECONDS = 2.0   # rate limit between requests
MAX_RETRIES = 2                  # retry twice after the initial attempt (3 total)
RETRY_DELAY_SECONDS = 5.0        # sleep between retries
REQUEST_TIMEOUT_SECONDS = 30.0


def _utc_now_iso() -> str:
    """Return current UTC time as 'YYYY-MM-DDTHH:MM:SSZ'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BaseScraper:
    """HTTP + HTML base class shared by all program-specific scrapers."""

    def __init__(
        self,
        *,
        scrape_log_path: Path | str = DEFAULT_SCRAPE_LOG_PATH,
        user_agent: str = DEFAULT_USER_AGENT,
        request_interval: float = REQUEST_INTERVAL_SECONDS,
        max_retries: int = MAX_RETRIES,
        retry_delay: float = RETRY_DELAY_SECONDS,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.scrape_log_path = Path(scrape_log_path)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.request_interval = request_interval
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.timeout = timeout
        self._last_request_at: float | None = None

    # ------------------------------------------------------------------ fetch

    def _throttle(self) -> None:
        """Sleep long enough to honour the minimum per-request interval."""
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        wait = self.request_interval - elapsed
        if wait > 0:
            time.sleep(wait)

    def fetch(self, url: str) -> BeautifulSoup | None:
        """Fetch `url` and return a parsed BeautifulSoup, or None on failure.

        The method always logs exactly one audit entry per call, regardless
        of how many retries were attempted under the hood. The logged entry
        reflects the final outcome (success on the attempt that worked, or
        the last error if every attempt failed).
        """
        attempts = self.max_retries + 1
        last_error: str | None = None
        last_status: int | None = None

        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                self._last_request_at = time.monotonic()
                last_error = f"{type(exc).__name__}: {exc}"
                last_status = None
            else:
                self._last_request_at = time.monotonic()
                last_status = response.status_code
                if 200 <= response.status_code < 300:
                    soup = BeautifulSoup(response.text, "lxml")
                    self.save_to_log(
                        {
                            "url": url,
                            "timestamp": _utc_now_iso(),
                            "status_code": response.status_code,
                            "success": True,
                            "error": None,
                        }
                    )
                    return soup
                last_error = f"HTTP {response.status_code}"

            # Sleep before the next retry, but not after the final attempt.
            if attempt < attempts:
                time.sleep(self.retry_delay)

        self.save_to_log(
            {
                "url": url,
                "timestamp": _utc_now_iso(),
                "status_code": last_status,
                "success": False,
                "error": last_error,
            }
        )
        return None

    # --------------------------------------------------------------- parsing

    @staticmethod
    def extract_text(soup: BeautifulSoup | None, selector: str) -> str | None:
        """Return clean, whitespace-normalised text for the first match.

        Returns None if soup is None, the selector matches nothing, or the
        matched element has no visible text.
        """
        if soup is None:
            return None
        element = soup.select_one(selector)
        if element is None:
            return None
        raw = element.get_text(" ", strip=True)
        collapsed = " ".join(raw.split())
        return collapsed or None

    @staticmethod
    def extract_links(soup: BeautifulSoup | None) -> list[str]:
        """Return every href on the page, in document order."""
        if soup is None:
            return []
        return [a["href"] for a in soup.find_all("a", href=True)]

    # ---------------------------------------------------------------- audit

    def save_to_log(self, entry: dict[str, Any]) -> None:
        """Append a single outcome entry to scrape_log.json."""
        self.scrape_log_path.parent.mkdir(parents=True, exist_ok=True)

        existing: list[dict[str, Any]] = []
        if self.scrape_log_path.exists():
            text = self.scrape_log_path.read_text(encoding="utf-8").strip()
            if text:
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    data = []
                if isinstance(data, list):
                    existing = data

        existing.append(entry)
        self.scrape_log_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test() -> None:
    """Fetch a single real URL and report the outcome.

    This is intentionally a smoke test, not a unit test. We actually hit the
    live UFA Foundation page once so we can confirm end-to-end that the
    session headers, parser, rate-limit, and log-append logic all cooperate.
    """
    scraper = BaseScraper()
    test_url = "https://ufafoundation.com/acf-programs-rc-grants/"

    soup = scraper.fetch(test_url)

    # Read the log entry this call just produced.
    log_entries: list[dict[str, Any]] = []
    if scraper.scrape_log_path.exists():
        raw = scraper.scrape_log_path.read_text(encoding="utf-8").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    log_entries = parsed
            except json.JSONDecodeError:
                log_entries = []
    last_entry = log_entries[-1] if log_entries else None

    success = soup is not None
    status_code = last_entry.get("status_code") if last_entry else None

    print(f"URL           : {test_url}")
    print(f"Fetch         : {'SUCCESS' if success else 'FAILED'}")
    print(f"HTTP status   : {status_code}")

    if success:
        body_text = BaseScraper.extract_text(soup, "body")
        if body_text is None:
            body_text = soup.get_text(" ", strip=True)
            body_text = " ".join(body_text.split())
        excerpt = (body_text or "")[:200]
        print(f"Text excerpt  : {excerpt!r}")
    else:
        err = last_entry.get("error") if last_entry else None
        print(f"Error         : {err}")

    print()
    print("scrape_log.json entry just written:")
    if last_entry is not None:
        print(json.dumps(last_entry, indent=2, ensure_ascii=False))
    else:
        print("(no log entry found)")


if __name__ == "__main__":
    _smoke_test()
