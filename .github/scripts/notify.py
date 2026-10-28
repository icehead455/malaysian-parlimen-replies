#!/usr/bin/env python3
"""Email a plain digest for newly fetched reply PDFs.

    python3 .github/scripts/notify.py <list-file>

<list-file> holds the repo-relative paths of the new PDFs, one per line
(as `git ls-files --others` prints them). For each file this reads the
`replies` table in parlimen.duckdb (built by `parlimen_replies.py run`)
and emails a mechanical digest: the PDF's creation time, the number of
replies, a keyword-sweep result for the watchlist terms, and the list of
questions (number, MP, ministry). No editorial judgement here; the
newsworthiness screening and story drafts stay with Claude.

Sends through SMTP using three environment variables:
  SMTP_USER  the Gmail address to send from (and its login)
  SMTP_PASS  a Gmail App Password (not the normal password)
  MAIL_TO    where to send (defaults to SMTP_USER)
Set NOTIFY_DRY_RUN=1 to print the digest and skip sending.
"""
import datetime as dt
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pdfplumber

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "parlimen.duckdb"
MYT = ZoneInfo("Asia/Kuala_Lumpur")
PDF_URL = "https://www.parlimen.gov.my/files/jindex/pdf/{}"

# The watchlist terms, same as the nightly screen: rare earths and the
# house arrest / Najib / 1MDB cluster. A hit means read that reply.
WATCH = re.compile(
    r"nadir bumi|rare earth|\bree\b|lynas|monazit|nr-lamp|"
    r"tahanan (dalam )?rumah|house arrest|najib|pengampunan|adendum|addendum",
    re.I,
)


def pdf_created(path):
    """The PDF's internal creation time in Malaysia time, or None."""
    try:
        raw = pdfplumber.open(path).metadata.get("CreationDate", "")
    except Exception:
        return None
    m = re.match(r"D:(\d{14})", raw or "")
    if not m:
        return None
    naive = dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    return naive.replace(tzinfo=MYT)  # the site stamps these in +08'00'


def digest_for(con, filename):
    rows = con.execute(
        "SELECT question_no, mp, constituency, ministry, answered_by, "
        "question, answer FROM replies WHERE filename = ? AND answered_with IS NULL "
        "ORDER BY seq",
        [filename],
    ).fetchall()
    hits = [r[0] for r in rows if WATCH.search((r[5] or "") + " " + (r[6] or ""))]

    created = pdf_created(ROOT / "inbox" / filename)
    found = dt.datetime.now(MYT)
    fmt = "%a %d %b %Y, %I:%M %p"
    lines = [f"=== {filename} ({PDF_URL.format(filename)})"]
    lines.append(
        f"PDF created {created.strftime(fmt) if created else '?'}; "
        f"found by this run {found.strftime(fmt)} (Malaysia time)"
    )
    lines.append(f"{len(rows)} replies.")
    lines.append(
        "Watchlist: " + (
            f"HIT on question(s) {', '.join(map(str, hits))} "
            "(rare earth / house arrest / Najib / pardon terms)"
            if hits else "no rare-earth or house-arrest/Najib mentions"
        )
    )
    lines.append("")
    for q, mp, con_, mi, by, _, _ in rows:
        who = f"{mp} [{con_}]" if con_ else (mp or "?")
        line = f"  Q{q}: {who} — {mi or '?'}"
        if by:
            line += f" (answered by {by})"
        lines.append(line)
    return "\n".join(lines), hits


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: notify.py <list-file>")
    names = [
        Path(p.strip()).name
        for p in Path(sys.argv[1]).read_text().splitlines()
        if p.strip().lower().endswith(".pdf")
    ]
    if not names:
        print("notify: no new PDFs in the list, nothing to send")
        return

    con = duckdb.connect(str(DB), read_only=True)
    blocks, any_hit = [], False
    for name in names:
        text, hits = digest_for(con, name)
        blocks.append(text)
        any_hit = any_hit or bool(hits)
    con.close()

    dates = ", ".join(sorted(names))
    subject = f"Parlimen replies: {len(names)} new file(s) — {dates}"
    if any_hit:
        subject = "[WATCHLIST] " + subject
    body = (
        "New Dewan Rakyat reply PDF(s) are on parlimen.gov.my and have been "
        "committed to the repo.\n\nThis is the mechanical GitHub Actions digest "
        "(no newsworthiness screening). Ask Claude for the full screen and story "
        "drafts.\n\n" + "\n\n".join(blocks) + "\n"
    )
    print(body)

    if os.environ.get("NOTIFY_DRY_RUN") == "1":
        print("notify: DRY RUN, not sending")
        return

    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to = os.environ.get("MAIL_TO") or user
    if not (user and pw and to):
        sys.exit("notify: SMTP_USER, SMTP_PASS (and MAIL_TO) must be set")

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.send_message(msg)
    print(f"notify: emailed {to}")


if __name__ == "__main__":
    main()
