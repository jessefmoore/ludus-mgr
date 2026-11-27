"""Render a VPN activity report to PDF.

Kept free of API/schema imports (takes plain dicts) so it can be called from
the endpoint layer without an import cycle. Uses fpdf2 - pure Python, no system
libraries, so it works in the slim runtime image.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from fpdf import FPDF

# Table columns: (header, dict-key, width in mm). Widths sum to ~277 (A4
# landscape printable width).
_COLUMNS: list[tuple[str, str, float]] = [
    ("User ID", "ludus_userid", 40),
    ("Name", "full_name", 55),
    ("Range", "range_id", 35),
    ("Range Group", "session_name", 50),
    ("Status", "status", 22),
    ("Logins", "login_count", 20),
    ("Last Seen", "last_seen", 55),
]


def render_overview_pdf(
    *,
    generated_at: datetime,
    summary: dict,
    rows: list[dict],
    period: str = "All time",
) -> bytes:
    """Build the VPN activity report PDF and return its bytes.

    ``summary`` carries the headline counts; ``rows`` is one pre-formatted dict
    per user with keys matching :data:`_COLUMNS`; ``period`` labels the
    login-count date range the report covers.
    """
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, "VPN Activity Report", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(110, 110, 110)
    pdf.cell(
        0, 6,
        f"Generated {generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        f"    |    Login counts: {period}",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(3)

    # Summary line.
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 11)
    most = summary.get("most_active_userid")
    most_txt = (
        f"{most} ({summary.get('most_active_logins', 0)})" if most else "-"
    )
    pdf.cell(
        0, 7,
        f"Users: {summary.get('total_users', 0)}    "
        f"Online now: {summary.get('online_now', 0)}    "
        f"Total logins: {summary.get('total_logins', 0)}    "
        f"Most active: {most_txt}",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(2)

    # Table header.
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(235, 235, 235)
    for header, _key, width in _COLUMNS:
        pdf.cell(width, 8, header, border=1, fill=True)
    pdf.ln()

    # Table rows.
    pdf.set_font("Helvetica", "", 9)
    if not rows:
        pdf.cell(0, 8, "No provisioned users.", border=1)
        pdf.ln()
    for row in rows:
        for _header, key, width in _COLUMNS:
            value = row.get(key, "")
            pdf.cell(width, 7, str(value), border=1)
        pdf.ln()

    # fpdf2 returns a bytearray; normalise to bytes for the HTTP response.
    return bytes(pdf.output())


# CSV gets an extra Email column that the width-constrained PDF omits.
_CSV_COLUMNS: list[tuple[str, str]] = [
    ("User ID", "ludus_userid"),
    ("Name", "full_name"),
    ("Email", "email"),
    ("Range", "range_id"),
    ("Range Group", "session_name"),
    ("Status", "status"),
    ("Logins", "login_count"),
    ("Last Seen", "last_seen"),
]


def render_overview_csv(rows: list[dict]) -> str:
    """Render the per-user activity rows as CSV text (header + one row each)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([header for header, _key in _CSV_COLUMNS])
    for row in rows:
        writer.writerow([row.get(key, "") for _header, key in _CSV_COLUMNS])
    return buf.getvalue()


__all__ = ["render_overview_csv", "render_overview_pdf"]
