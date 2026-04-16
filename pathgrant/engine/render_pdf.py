"""
pathgrant/engine/render_pdf.py

Locked PDF renderer for PathGrant reports. Converts latest.md to a
production-quality PDF using WeasyPrint. Replaces Gamma permanently.

Design spec is locked: copper/charcoal palette, Georgia body, Arial
headings, US Letter, 1-inch margins, inline SVG icons. No external
dependencies beyond weasyprint, markdown, and pygments.

Part 1: constants, SVG icon library, stat extractor, CSS stylesheet.
Part 2 (next): HTML renderer, cover page builder, CLI, inline tests.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

_ENGINE_ROOT = Path(__file__).resolve().parent
_PATHGRANT_ROOT = _ENGINE_ROOT.parent
_REPORTS_DIR = _PATHGRANT_ROOT / "reports"


# ---------------------------------------------------------------------------
# Color palette -- HARDCODED, NO EXCEPTIONS
# Prohibited: pink, blue, teal, green, yellow
# ---------------------------------------------------------------------------

COPPER = "#B87333"
DARK = "#1a1a1a"
CHARCOAL = "#2b2b2b"
GREY_BG = "#f5f5f5"
WHITE = "#ffffff"
LIGHT_COPPER_BG = "#fdf6ec"
MUTED = "#888888"


# ---------------------------------------------------------------------------
# Page dimensions -- US Letter portrait, 1 inch margins
# ---------------------------------------------------------------------------

PAGE_WIDTH = "8.5in"
PAGE_HEIGHT = "11in"
PAGE_MARGIN = "1in"


# ---------------------------------------------------------------------------
# SVG icon library -- inline, no external dependencies
#
# All icons: 20x20 viewBox, stroke-based (not fill), color #B87333.
# Cover page logos are larger and use their own viewBox.
# ---------------------------------------------------------------------------

ICONS: dict[str, str] = {
    # Clock -- circle with two hands (alerts / deadlines)
    "clock": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" '
        'width="20" height="20" fill="none" stroke="#B87333" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="10" cy="10" r="8"/>'
        '<polyline points="10,5 10,10 13,12.5"/>'
        '</svg>'
    ),

    # Shield -- eligibility risks
    "shield": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" '
        'width="20" height="20" fill="none" stroke="#B87333" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M10,2 L17,5 L17,10 C17,14.5 10,18 10,18 '
        'C10,18 3,14.5 3,10 L3,5 Z"/>'
        '</svg>'
    ),

    # Target -- top grants / matches
    "target": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" '
        'width="20" height="20" fill="none" stroke="#B87333" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="10" cy="10" r="8"/>'
        '<circle cx="10" cy="10" r="5"/>'
        '<circle cx="10" cy="10" r="2"/>'
        '</svg>'
    ),

    # Calendar -- 90-day action plan
    "calendar": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" '
        'width="20" height="20" fill="none" stroke="#B87333" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
        '<rect x="3" y="4" width="14" height="13" rx="1.5"/>'
        '<line x1="3" y1="8" x2="17" y2="8"/>'
        '<line x1="7" y1="2" x2="7" y2="5"/>'
        '<line x1="13" y1="2" x2="13" y2="5"/>'
        '</svg>'
    ),

    # Document -- required documents
    "document": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" '
        'width="20" height="20" fill="none" stroke="#B87333" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M5,2 L12,2 L16,6 L16,18 L5,18 Z"/>'
        '<polyline points="12,2 12,6 16,6"/>'
        '<line x1="7" y1="10" x2="14" y2="10"/>'
        '<line x1="7" y1="13" x2="14" y2="13"/>'
        '</svg>'
    ),

    # Star -- recommended engagement option
    "star": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" '
        'width="20" height="20" fill="none" stroke="#B87333" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
        '<polygon points="10,2 12.5,7.5 18,8 14,12 15,17.5 10,15 '
        '5,17.5 6,12 2,8 7.5,7.5"/>'
        '</svg>'
    ),

    # Check -- completed / clean items
    "check": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" '
        'width="20" height="20" fill="none" stroke="#B87333" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<polyline points="4,10 8,14 16,6"/>'
        '</svg>'
    ),

    # Stragentic logo -- copper circle with upward trend bars (cover page)
    "stragentic_logo": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80" '
        'width="80" height="80" fill="none" stroke="#B87333" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="40" cy="40" r="36"/>'
        '<line x1="22" y1="55" x2="22" y2="45"/>'
        '<line x1="30" y1="55" x2="30" y2="40"/>'
        '<line x1="38" y1="55" x2="38" y2="35"/>'
        '<line x1="46" y1="55" x2="46" y2="30"/>'
        '<line x1="54" y1="55" x2="54" y2="25"/>'
        '<polyline points="22,43 30,38 38,33 46,28 54,23" '
        'stroke-width="1.5"/>'
        '</svg>'
    ),

    # PathGrant logo -- location pin with route path (cover page)
    "pathgrant_logo": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" '
        'width="48" height="48" fill="none" stroke="#B87333" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M24,4 C17.4,4 12,9.4 12,16 C12,26 24,40 24,40 '
        'C24,40 36,26 36,16 C36,9.4 30.6,4 24,4 Z"/>'
        '<circle cx="24" cy="16" r="5"/>'
        '<path d="M20,16 L24,12 L28,16 L24,20 Z" '
        'stroke-width="1.2"/>'
        '</svg>'
    ),
}


# ---------------------------------------------------------------------------
# Cover page stat extractor
# ---------------------------------------------------------------------------

def extract_cover_stats(md_text: str) -> dict[str, str]:
    """Parse the markdown report to extract three cover-page statistics.

    Returns a dict with keys:
      grants_count   -- number of scored grants (from Complete Grant Register)
      top_amount     -- highest funding amount (from first Tier 1 grant)
      days_to_deadline -- days remaining for the most urgent deadline

    All values are strings ready for display. Returns "N/A" for any
    value that cannot be extracted.
    """
    stats: dict[str, str] = {
        "grants_count": "N/A",
        "top_amount": "N/A",
        "days_to_deadline": "N/A",
    }

    # Grants count: "All 14 verified programs scored for" in the
    # Complete Grant Register intro line.
    m = re.search(r"All (\d+) verified programs scored", md_text)
    if m:
        stats["grants_count"] = m.group(1)

    # Top amount: first amount line in the report body after a Tier 1
    # header. Matches patterns like "up to $400,000" or "$10,000-$50,000"
    # or "from $250".
    m = re.search(
        r"(?:up to |from )?\$[\d,]+(?:\s*[–-]\s*\$[\d,]+)?",
        md_text,
    )
    if m:
        stats["top_amount"] = m.group(0)

    # Days to deadline: "N days away" from the Alerts section.
    m = re.search(r"(\d+) days away", md_text)
    if m:
        stats["days_to_deadline"] = m.group(1)
    else:
        # Check for "is today"
        if "is today" in md_text:
            stats["days_to_deadline"] = "0"

    return stats


# ---------------------------------------------------------------------------
# CSS stylesheet -- locked design spec
# ---------------------------------------------------------------------------

CSS = f"""
/* ================================================================
   PathGrant PDF Stylesheet -- locked design spec
   Colors: copper #B87333, dark #1a1a1a, charcoal #2b2b2b
   Typography: Georgia body, Arial headings, Courier New monospace
   Page: US Letter portrait, 1 inch margins
   ================================================================ */

/* --- Page setup --- */
@page {{
    size: letter portrait;
    margin: {PAGE_MARGIN};

    @bottom-center {{
        content: "Page " counter(page) " of " counter(pages);
        font-family: Arial, Helvetica, sans-serif;
        font-size: 8pt;
        color: {MUTED};
    }}
}}

/* First page (cover) has no header or footer */
@page :first {{
    @top-left {{ content: none; }}
    @bottom-center {{ content: none; }}
}}

/* Subsequent pages get the running header */
@page :not(:first) {{
    @top-left {{
        content: none;
    }}
}}

/* --- Base typography --- */
body {{
    font-family: Georgia, "Times New Roman", serif;
    font-size: 10pt;
    line-height: 1.5;
    color: {CHARCOAL};
    -weasyprint-hyphens: auto;
}}

/* --- Headings --- */
h1, h2, h3, h4 {{
    font-family: Arial, Helvetica, sans-serif;
    font-weight: bold;
    color: {CHARCOAL};
    page-break-after: avoid;
    margin-top: 18pt;
    margin-bottom: 6pt;
}}

h1 {{
    font-size: 20pt;
    color: {COPPER};
    border-bottom: 2px solid {COPPER};
    padding-bottom: 4pt;
}}

h2 {{
    font-size: 14pt;
    border-bottom: 1px solid {COPPER};
    padding-bottom: 3pt;
    margin-top: 24pt;
}}

h3 {{
    font-size: 11pt;
    color: {CHARCOAL};
    margin-top: 14pt;
}}

h4 {{
    font-size: 10pt;
    color: {COPPER};
    margin-top: 10pt;
}}

/* --- Monospace (grant IDs, scores) --- */
code {{
    font-family: "Courier New", Courier, monospace;
    font-size: 8.5pt;
    background-color: {GREY_BG};
    padding: 1pt 3pt;
    border-radius: 2pt;
}}

/* --- Links --- */
a {{
    color: {COPPER};
    text-decoration: none;
}}

/* --- Horizontal rules --- */
hr {{
    border: none;
    border-top: 1px solid {COPPER};
    margin: 16pt 0;
}}

/* --- Block quotes (used for sentinel warnings) --- */
blockquote {{
    border-left: 3px solid {COPPER};
    padding-left: 12pt;
    margin-left: 0;
    color: {MUTED};
    font-style: italic;
}}

/* --- Lists --- */
ul, ol {{
    margin-left: 0;
    padding-left: 18pt;
}}

li {{
    margin-bottom: 3pt;
}}

/* --- Tables (general) --- */
table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 8.5pt;
    margin: 10pt 0;
    page-break-inside: auto;
}}

th {{
    background-color: {DARK};
    color: {WHITE};
    font-family: Arial, Helvetica, sans-serif;
    font-weight: bold;
    text-align: left;
    padding: 6pt 8pt;
    border-bottom: 2px solid {COPPER};
}}

td {{
    padding: 5pt 8pt;
    border-bottom: 1px solid #e0e0e0;
    vertical-align: top;
}}

/* Alternating row colors */
tr:nth-child(even) td {{
    background-color: {GREY_BG};
}}

/* --- Grant register table locked column widths --- */
table.grant-register th:nth-child(1),
table.grant-register td:nth-child(1) {{ width: 28%; }}
table.grant-register th:nth-child(2),
table.grant-register td:nth-child(2) {{ width: 18%; }}
table.grant-register th:nth-child(3),
table.grant-register td:nth-child(3) {{ width: 14%; }}
table.grant-register th:nth-child(4),
table.grant-register td:nth-child(4) {{ width: 8%; font-weight: bold; color: {COPPER}; }}
table.grant-register th:nth-child(5),
table.grant-register td:nth-child(5) {{ width: 6%; text-align: center; }}
table.grant-register th:nth-child(6),
table.grant-register td:nth-child(6) {{ width: 8%; text-align: center; }}
table.grant-register th:nth-child(7),
table.grant-register td:nth-child(7) {{ width: 18%; }}

/* Tier 2 scores: charcoal */
table.grant-register tr.tier-2 td:nth-child(4) {{
    color: {CHARCOAL};
}}

/* Negative / Tier 3 scores: muted */
table.grant-register tr.tier-3 td:nth-child(4) {{
    color: {MUTED};
    font-weight: normal;
}}

/* --- Score badge --- */
.score-badge {{
    display: inline-block;
    background-color: {COPPER};
    color: {WHITE};
    font-family: Arial, Helvetica, sans-serif;
    font-size: 9pt;
    font-weight: bold;
    padding: 2pt 8pt;
    border-radius: 10pt;
}}

/* --- Deadline badge --- */
.deadline-badge {{
    display: inline-block;
    background-color: {DARK};
    color: {WHITE};
    font-family: Arial, Helvetica, sans-serif;
    font-size: 8pt;
    padding: 2pt 6pt;
    border-radius: 3pt;
}}

/* --- Stackable tag --- */
.stackable-tag {{
    display: inline-block;
    border: 1px solid {COPPER};
    color: {COPPER};
    font-family: Arial, Helvetica, sans-serif;
    font-size: 7pt;
    padding: 1pt 4pt;
    border-radius: 2pt;
    text-transform: uppercase;
}}

/* --- Alert box --- */
.alert-box {{
    border-left: 4px solid {COPPER};
    background-color: {LIGHT_COPPER_BG};
    padding: 10pt 14pt;
    margin: 8pt 0;
    page-break-inside: avoid;
}}

.alert-box .alert-date {{
    color: {COPPER};
    font-weight: bold;
}}

.alert-box .alert-date-urgent {{
    color: #c0392b;
    font-weight: bold;
}}

/* --- Eligibility risk block --- */
.risk-block {{
    background-color: {CHARCOAL};
    color: {WHITE};
    padding: 10pt 14pt;
    margin: 8pt 0;
    border-radius: 3pt;
    page-break-inside: avoid;
}}

.risk-block .hard-stop {{
    color: {COPPER};
    font-weight: bold;
    font-family: Arial, Helvetica, sans-serif;
    text-transform: uppercase;
}}

/* --- Engagement option cards --- */
.option-card {{
    border: 1px solid {CHARCOAL};
    padding: 12pt 14pt;
    margin: 8pt 0;
    page-break-inside: avoid;
}}

.option-card.recommended {{
    border: 2px solid {COPPER};
    border-left-width: 4px;
}}

.option-card .recommended-badge {{
    display: inline-block;
    background-color: {COPPER};
    color: {WHITE};
    font-family: Arial, Helvetica, sans-serif;
    font-size: 8pt;
    font-weight: bold;
    padding: 2pt 8pt;
    border-radius: 2pt;
    text-transform: uppercase;
    margin-bottom: 6pt;
}}

/* --- 90-day action plan phase headers --- */
.phase-header {{
    background-color: {COPPER};
    color: {WHITE};
    font-family: Arial, Helvetica, sans-serif;
    font-weight: bold;
    padding: 6pt 12pt;
    margin-top: 12pt;
    margin-bottom: 6pt;
    page-break-after: avoid;
}}

/* --- DIY starter kit --- */
.diy-step-number {{
    color: {COPPER};
    font-weight: bold;
    font-family: Arial, Helvetica, sans-serif;
}}

.diy-time {{
    color: {COPPER};
    font-style: italic;
}}

/* --- Section icon --- */
.section-icon {{
    display: inline-block;
    vertical-align: middle;
    margin-right: 6pt;
    position: relative;
    top: -1pt;
}}

/* --- Cover page --- */
.cover-page {{
    text-align: center;
    page-break-after: always;
    padding-top: 60pt;
}}

.cover-logo {{
    margin-bottom: 16pt;
}}

.cover-brand {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 18pt;
    font-weight: bold;
    color: {COPPER};
    letter-spacing: 4pt;
    text-transform: uppercase;
    margin: 8pt 0 4pt 0;
}}

.cover-subtitle {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 14pt;
    color: {CHARCOAL};
    margin: 4pt 0 20pt 0;
}}

.cover-client {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 24pt;
    font-weight: bold;
    color: {COPPER};
    margin: 20pt 0 8pt 0;
}}

.cover-date {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 9pt;
    color: {MUTED};
    margin-bottom: 20pt;
}}

.cover-rule {{
    border: none;
    border-top: 1px solid {COPPER};
    margin: 20pt 0;
}}

/* Stat boxes row */
.stat-boxes {{
    display: flex;
    justify-content: center;
    gap: 12pt;
    margin: 20pt 0;
}}

.stat-box {{
    flex: 1;
    max-width: 180pt;
    background-color: {DARK};
    border-top: 4px solid {COPPER};
    padding: 16pt;
    text-align: center;
}}

.stat-box .stat-value {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 22pt;
    font-weight: bold;
    color: {WHITE};
    margin-bottom: 4pt;
}}

.stat-box .stat-value.copper {{
    color: {COPPER};
}}

.stat-box .stat-label {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 8pt;
    color: {MUTED};
    text-transform: uppercase;
    letter-spacing: 0.5pt;
}}

.cover-powered {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 9pt;
    font-style: italic;
    color: {MUTED};
    margin-top: 24pt;
}}

.cover-pathgrant-logo {{
    margin-top: 12pt;
}}

/* --- Running header (injected via page margin box workaround) --- */
.running-header {{
    display: none;
}}

/* Page break helpers */
.page-break {{
    page-break-before: always;
}}

/* Keep grant blocks together when possible */
.grant-block {{
    page-break-inside: avoid;
}}

/* Sources table: smaller font */
table.sources-table {{
    font-size: 7.5pt;
}}

table.sources-table td:first-child {{
    font-family: "Courier New", Courier, monospace;
    font-size: 7pt;
}}
"""


# ---------------------------------------------------------------------------
# Markdown to HTML helper
# ---------------------------------------------------------------------------

def _md_to_html(md_text: str) -> str:
    """Convert a markdown string to HTML using the markdown library.

    Uses the tables and fenced_code extensions so grant register tables
    and code blocks render correctly.
    """
    import markdown as _md

    return _md.markdown(
        md_text,
        extensions=["tables", "fenced_code"],
        output_format="html",
    )


# ---------------------------------------------------------------------------
# Section renderers (Part 2a)
# ---------------------------------------------------------------------------

def render_cover(
    stats: dict[str, str],
    client_name: str,
    report_date: str,
) -> str:
    """Build the cover page HTML.

    Layout top to bottom, all centered:
    1. Stragentic logo SVG (80px)
    2. STRAGENTIC brand text
    3. Grant Funding Analysis subtitle
    4. Client name (large, copper)
    5. Date line
    6. Copper horizontal rule
    7. Three stat boxes side by side
    8. Powered by PathGrant line
    9. PathGrant logo SVG (48px)
    """
    grants_count = stats.get("grants_count", "N/A")
    top_amount = stats.get("top_amount", "N/A")
    days = stats.get("days_to_deadline", "N/A")

    return f"""
<div class="cover-page">
  <div class="cover-logo">{ICONS["stragentic_logo"]}</div>

  <div class="cover-brand">STRAGENTIC</div>

  <div class="cover-subtitle">Grant Funding Analysis</div>

  <div class="cover-client">{client_name}</div>

  <div class="cover-date">
    Comprehensive Funding Intelligence Report &middot; {report_date}
  </div>

  <hr class="cover-rule"/>

  <div class="stat-boxes">
    <div class="stat-box">
      <div class="stat-value">{grants_count}</div>
      <div class="stat-label">Grants Analyzed</div>
    </div>
    <div class="stat-box">
      <div class="stat-value copper">{top_amount}</div>
      <div class="stat-label">Top Funding Amount</div>
    </div>
    <div class="stat-box">
      <div class="stat-value copper">{days}</div>
      <div class="stat-label">Days to Deadline</div>
    </div>
  </div>

  <div class="cover-powered">
    Powered by PathGrant &mdash; A Stragentic Service
  </div>

  <div class="cover-pathgrant-logo">{ICONS["pathgrant_logo"]}</div>
</div>
"""


def render_alerts(content: str) -> str:
    """Render the Alerts section with clock icon, copper left border,
    light copper background. Boldens deadline dates found in the text."""
    html_body = _md_to_html(content)

    # Bold any date pattern like "2026-05-31" or "April 15, 2026"
    html_body = re.sub(
        r"(\d{4}-\d{2}-\d{2})",
        r'<strong class="alert-date">\1</strong>',
        html_body,
    )
    # Bold "N days away" phrases
    html_body = re.sub(
        r"(\d+ days away)",
        r'<strong class="alert-date">\1</strong>',
        html_body,
    )
    # Bold "is today"
    html_body = html_body.replace(
        "is today",
        '<strong class="alert-date-urgent">is today</strong>',
    )

    icon = f'<span class="section-icon">{ICONS["clock"]}</span>'

    return f"""
<div class="alert-box">
  <h2>{icon} Alerts: Time-Sensitive Deadlines</h2>
  {html_body}
</div>
"""


def render_grant_deep_dive(content: str) -> str:
    """Render a full grant block with intelligence content.

    Applies visual enhancements:
    - Score badge (copper bg, white text) extracted from the ### header
    - Deadline badge (dark bg, white text) extracted from close date
    - Target icon on the grant header
    - Eligibility risks in dark charcoal block with shield icon
    - DIY steps with copper step numbers
    - Time commitment in copper italic
    """
    html_body = _md_to_html(content)
    icon_target = f'<span class="section-icon">{ICONS["target"]}</span>'
    icon_shield = f'<span class="section-icon">{ICONS["shield"]}</span>'
    icon_doc = f'<span class="section-icon">{ICONS["document"]}</span>'

    # Inject target icon into ### Tier headers
    html_body = re.sub(
        r"<h3>(Tier \d+ [^<]+)</h3>",
        rf"<h3>{icon_target}\1</h3>",
        html_body,
    )

    # Extract and badge-ify score from "Score NN" pattern in h3
    def _score_badge(m: re.Match) -> str:
        before = m.group(1)
        score = m.group(2)
        after = m.group(3)
        badge = f'<span class="score-badge">Score {score}</span>'
        # Strip the "Score NN" text from the heading, put badge after
        cleaned = re.sub(r"\*\*Score \d+\*\*\s*", "", before)
        return f"<h3>{cleaned}{badge}{after}</h3>"

    html_body = re.sub(
        r"<h3>(.*?)\*\*Score (\d+)\*\*(.*?)</h3>",
        _score_badge,
        html_body,
    )
    # Also handle already-converted <strong>Score NN</strong>
    html_body = re.sub(
        r"<h3>(.*?)<strong>Score (\d+)</strong>(.*?)</h3>",
        _score_badge,
        html_body,
    )

    # Badge-ify "close YYYY-MM-DD" deadline references in headers
    html_body = re.sub(
        r"close (\d{4}-\d{2}-\d{2})",
        r'<span class="deadline-badge">close \1</span>',
        html_body,
    )

    # Badge-ify "rolling intake"
    html_body = re.sub(
        r"rolling intake",
        r'<span class="deadline-badge">rolling intake</span>',
        html_body,
    )

    # Tag stackable markers
    html_body = re.sub(
        r"stackable",
        r'<span class="stackable-tag">stackable</span>',
        html_body,
        count=0,
        flags=re.IGNORECASE,
    )

    # Wrap eligibility risk paragraphs in dark blocks
    # Match the bold header "Eligibility risks:" through to the next bold
    # header or end of section
    html_body = re.sub(
        r"(<strong>Eligibility risks:</strong>)(.*?)(?=<strong>|<h[234]|<hr|$)",
        (
            rf'<div class="risk-block">'
            rf'{icon_shield} \1\2'
            rf'</div>'
        ),
        html_body,
        flags=re.DOTALL,
    )

    # Mark "HARD STOP" and "CRITICAL" in copper bold
    html_body = re.sub(
        r"\b(HARD STOP|CRITICAL)\b",
        r'<span class="hard-stop">\1</span>',
        html_body,
    )

    # Style DIY step numbers: "1." at start of list items
    html_body = re.sub(
        r"<li>\s*<strong>(\d+\.)",
        r'<li><strong><span class="diy-step-number">\1</span>',
        html_body,
    )

    # Style time commitment lines
    html_body = re.sub(
        r"(<h4>Time commitment</h4>\s*<p>)(.*?)(</p>)",
        r'\1<span class="diy-time">\2</span>\3',
        html_body,
        flags=re.DOTALL,
    )

    # Wrap required documents with document icon
    html_body = re.sub(
        r"(<h4>Required documents</h4>)",
        rf'\1<p>{icon_doc}</p>',
        html_body,
    )

    return f'<div class="grant-block">{html_body}</div>'


def render_grant_table(content: str) -> str:
    """Render the Complete Grant Register as a styled HTML table.

    Applies the grant-register class for locked column widths and
    adds tier-specific row classes for score coloring.
    """
    html_body = _md_to_html(content)

    # Add the grant-register class to the table element
    html_body = html_body.replace("<table>", '<table class="grant-register">')

    # Add tier classes to rows based on score value in the 4th column.
    # Parse each <tr> that has <td> cells, read the score, tag accordingly.
    def _tag_tier_row(m: re.Match) -> str:
        row_html = m.group(0)
        # Extract score from the 4th <td>
        tds = re.findall(r"<td>(.*?)</td>", row_html)
        if len(tds) >= 4:
            try:
                score = int(tds[3].strip())
                if score >= 60:
                    return row_html.replace("<tr>", '<tr class="tier-1">')
                elif score >= 30:
                    return row_html.replace("<tr>", '<tr class="tier-2">')
                else:
                    return row_html.replace("<tr>", '<tr class="tier-3">')
            except ValueError:
                pass
        return row_html

    html_body = re.sub(r"<tr>\s*<td>.*?</tr>", _tag_tier_row, html_body, flags=re.DOTALL)

    return html_body


def render_90_day_plan(content: str) -> str:
    """Render the 90-Day Action Plan with copper phase headers and
    calendar icons. Owner names rendered in copper italic."""
    html_body = _md_to_html(content)
    icon = f'<span class="section-icon">{ICONS["calendar"]}</span>'

    # Convert ### phase headers to copper-background phase blocks
    html_body = re.sub(
        r"<h3>(Weeks? [\d\-–]+)</h3>",
        rf'<div class="phase-header">{icon} \1</div>',
        html_body,
    )
    html_body = re.sub(
        r"<h3>(Decision gates)</h3>",
        rf'<div class="phase-header">{icon} \1</div>',
        html_body,
    )
    html_body = re.sub(
        r"<h3>(Responsible parties)</h3>",
        rf'<div class="phase-header">{icon} \1</div>',
        html_body,
    )

    # Style owner names: **Role Name** at start of bullet points
    # Make the bold role text copper italic
    html_body = re.sub(
        r"<strong>(Founder[^<]*?|Financial Advisor[^<]*?|Cultural Advisor[^<]*?"
        r"|Legal Advisor[^<]*?|Educational Consultant[^<]*?"
        r"|Hockey Director[^<]*?|Hockey Advisor[^<]*?"
        r"|Business Advisor[^<]*?|Writing lead[^<]*?)</strong>",
        r'<strong style="color: #B87333; font-style: italic;">\1</strong>',
        html_body,
    )

    return html_body


def render_engagement_options(content: str) -> str:
    """Render Engagement Options with recommended option highlighted.

    Option C (recommended) gets copper border + star icon + RECOMMENDED badge.
    Other options get charcoal border.
    """
    html_body = _md_to_html(content)
    icon_star = f'<span class="section-icon">{ICONS["star"]}</span>'

    # Find the recommended option block (contains the star emoji or "Recommended")
    # and wrap it in the recommended card class
    html_body = re.sub(
        r"(<h3>)(.*?Recommended.*?)(</h3>)(.*?)(?=<h3>|<h2>|<strong>Why Option|$)",
        (
            rf'<div class="option-card recommended">'
            rf'<div class="recommended-badge">{icon_star} RECOMMENDED</div>'
            rf'\1\2\3\4</div>'
        ),
        html_body,
        flags=re.DOTALL,
    )

    # Wrap non-recommended option blocks
    # Match h3 headers for Option A and Option B
    html_body = re.sub(
        r"(<h3>(?:Option [AB][^<]*?)</h3>)(.*?)(?=<h3>|<div class=\"option-card|<strong>Why Option|<strong>ROI|$)",
        r'<div class="option-card">\1\2</div>',
        html_body,
        flags=re.DOTALL,
    )

    return html_body


def render_sources_table(content: str) -> str:
    """Render the Sources table with compact styling."""
    html_body = _md_to_html(content)
    html_body = html_body.replace("<table>", '<table class="sources-table">')
    return html_body


def render_generic_section(content: str) -> str:
    """Fallback renderer: convert markdown to HTML, preserve structure.

    Used for sections without special visual treatment (Client Snapshot,
    Research Queue, Advisory Notes, Methodology, etc.).
    """
    return _md_to_html(content)


# ---------------------------------------------------------------------------
# Module-level verification: importable without side effects
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("render_pdf.py Part 1 loaded successfully")
    print(f"  Icons: {len(ICONS)}")
    print(f"  CSS length: {len(CSS)} chars")

    test_md = (
        "All 14 verified programs scored for The Emerge Academy.\n"
        "up to $400,000\n"
        "Deadline 2026-05-31 is 45 days away\n"
    )
    stats = extract_cover_stats(test_md)
    print(f"  Stats extraction test: {stats}")
