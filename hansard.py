#!/usr/bin/env python3
"""Dewan Rakyat Hansard attendance collector.

Downloads the daily Dewan Rakyat Hansard (Penyata Rasmi) PDFs from
www.parlimen.gov.my and extracts the attendance front matter — the
"Ahli-Ahli Yang Hadir" (present) and "Ahli-Ahli Yang Tidak Hadir" (absent)
lists that open every sitting day's Hansard — into a per-sitting-day
attendance table, keyed by constituency (the stable identifier for a
Dewan Rakyat seat).

    python3 hansard.py fetch           # download new DR-<ddmmyyyy>.pdf into hansard/
    python3 hansard.py parse           # (re)build attendance.csv from hansard/*.pdf
    python3 hansard.py run             # fetch, then parse
    python3 hansard.py report          # per-MP attendance summary across sitting days

Only publicly served pages are read. www.parlimen.gov.my omits its TLS
intermediate, so certs/ supplies it (same as parlimen_replies.py); TLS
verification stays on. Raw Hansard PDFs are kept byte for byte under
hansard/ and are never edited.
"""
import csv
import datetime as dt
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import certifi
import pdfplumber
import requests

HERE = Path(__file__).resolve().parent
CERTS = HERE / "certs"
HANSARD_DIR = HERE / "hansard"
ATT_CSV = HERE / "attendance.csv"

SITE = "https://www.parlimen.gov.my"
LISTING = SITE + "/hansard-dewan-rakyat.html?uweb=dr&"
# Hansard PDFs are linked as /files/hindex/pdf/DR-<ddmmyyyy>.pdf
PDF_LINK = r"""(?:href=["']|loadResult\(\s*')([^'"]*?/files/hindex/pdf/DR-\d{8}\.pdf)['"]"""

# Front-matter section markers (capitals vary; matched case-insensitively).
RE_HADIR = re.compile(r"Ahli-?Ahli\s+Yang\s+Hadir\s*:", re.I)
RE_TURUT = re.compile(r"Turut\s+Hadir\s*:", re.I)
RE_TIDAK = re.compile(r"Ahli-?Ahli\s+Yang\s+Tidak\s+Hadir\s*:", re.I)
RE_START = re.compile(r"Mesyuarat\s+dimulakan", re.I)       # proceedings begin here
RE_PAGEHDR = re.compile(r"(?mi)^\s*DR\.\s*\d{1,2}\.\d{1,2}\.\d{4}\s+[ivxlcdm]+\s*$")
RE_ITEM = re.compile(r"(?m)^\s*(\d{1,3})\.\s+")


def ca_bundle():
    """The usual CA bundle plus certs/*.pem (the site's missing intermediate)."""
    base = os.environ.get("REQUESTS_CA_BUNDLE") or certifi.where()
    out = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
    out.write(Path(base).read_text())
    for pem in sorted(CERTS.glob("*.pem")):
        out.write("\n" + pem.read_text())
    out.close()
    return out.name


def listing_urls(session, verify):
    r = session.get(LISTING, timeout=60, verify=verify)
    r.raise_for_status()
    urls = [urljoin(LISTING, m) for m in re.findall(PDF_LINK, r.text, re.I)]
    return list(dict.fromkeys(urls))


def fetch():
    """Download every listed Hansard PDF not already in hansard/."""
    HANSARD_DIR.mkdir(exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "parlimen-hansard/1.0"
    verify = ca_bundle()  # requests lets REQUESTS_CA_BUNDLE override session.verify, so pass per request
    urls = listing_urls(session, verify)
    print(f"{LISTING}: {len(urls)} Hansard link(s)")
    have = {p.name.lower() for p in HANSARD_DIR.iterdir()} if HANSARD_DIR.exists() else set()
    new = 0
    for url in urls:
        name = Path(unquote(urlparse(url).path)).name
        if not re.fullmatch(r"DR-\d{8}\.pdf", name, re.I):
            continue
        if name.lower() in have:
            continue
        try:
            r = session.get(url, timeout=180, verify=verify)
            r.raise_for_status()
        except Exception as e:
            print(f"FETCH FAILED {url}: {e}", file=sys.stderr)
            continue
        (HANSARD_DIR / name).write_bytes(r.content)
        print(f"fetched {name} ({len(r.content)} bytes)")
        new += 1
    print(f"{new} new Hansard file(s); {len(list(HANSARD_DIR.glob('DR-*.pdf')))} on disk")
    return new


def _date_from_name(name):
    m = re.fullmatch(r"DR-(\d{2})(\d{2})(\d{4})\.pdf", name, re.I)
    if not m:
        return None
    d, mth, y = map(int, m.groups())
    try:
        return dt.date(y, mth, d)
    except ValueError:
        return None


def _front_matter(pdf):
    """Concatenated text of the opening pages, truncated where debate begins."""
    buf = []
    for page in pdf.pages[:20]:
        t = page.extract_text() or ""
        buf.append(t)
        if RE_START.search(t):
            break
    txt = "\n".join(buf)
    m = RE_START.search(txt)
    if m:
        txt = txt[: m.start()]
    return RE_PAGEHDR.sub("", txt)


def _section(txt, start_re, end_res):
    m = start_re.search(txt)
    if not m:
        return None
    rest = txt[m.end():]
    ends = [e.start() for e in (r.search(rest) for r in end_res) if e]
    return rest[: min(ends)] if ends else rest


def _entries(sec):
    """(number, single-line body) for each numbered item in a section."""
    if not sec:
        return []
    parts = re.split(RE_ITEM, sec)
    out = []
    for i in range(1, len(parts) - 1, 2):
        body = re.sub(r"\s+", " ", parts[i + 1]).strip()
        out.append((int(parts[i]), body))
    return out


def _seat(body):
    """(name, constituency) for a Dewan Rakyat member line, or None.

    Senators (Dewan Negara) carry no constituency and are skipped. The
    constituency is the last parenthesis; any earlier parenthesis is a
    ministerial portfolio.
    """
    if "Senator" in body:
        return None
    parens = re.findall(r"\(([^)]*)\)", body)
    if not parens:
        return None
    constituency = re.sub(r"\s+", " ", parens[-1]).strip()
    head = body[: body.rfind("(")].strip().rstrip(",").strip()
    name = head.split(", ")[-1].strip() if ", " in head else head
    return name, constituency


def parse_pdf(path):
    """{date, present:[(name,con)], absent:[(name,con)], bil} for one Hansard."""
    date = _date_from_name(Path(path).name)
    with pdfplumber.open(str(path)) as pdf:
        txt = _front_matter(pdf)
    bil = None
    mbil = re.search(r"Bil\.\s*(\d+)", txt)
    if mbil:
        bil = int(mbil.group(1))
    hadir = _section(txt, RE_HADIR, [RE_TURUT, RE_TIDAK])
    tidak = _section(txt, RE_TIDAK, [])
    present = [s for s in (_seat(b) for _, b in _entries(hadir)) if s]
    absent = [s for s in (_seat(b) for _, b in _entries(tidak)) if s]
    return {"date": date, "bil": bil, "present": present, "absent": absent}


def parse_all():
    """Rebuild attendance.csv from every hansard/DR-*.pdf (idempotent)."""
    rows = []
    files = sorted(HANSARD_DIR.glob("DR-*.pdf"))
    for p in files:
        rec = parse_pdf(p)
        if rec["date"] is None:
            print(f"SKIP {p.name}: bad date", file=sys.stderr)
            continue
        if not rec["present"] and not rec["absent"]:
            print(f"WARN {p.name}: no attendance lists found", file=sys.stderr)
        seen = {}
        for status, lst in (("present", rec["present"]), ("absent", rec["absent"])):
            for name, con in lst:
                if con in seen:
                    continue  # a seat should appear once; keep the first
                seen[con] = True
                rows.append((rec["date"].isoformat(), rec["bil"] or "", status, con, name))
        print(f"{p.name}: {rec['date']} Bil.{rec['bil']} "
              f"present={len(rec['present'])} absent={len(rec['absent'])} "
              f"total={len(rec['present']) + len(rec['absent'])}")
    rows.sort(key=lambda r: (r[0], r[2] != "present", r[3]))
    with open(ATT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sitting_date", "bil", "status", "constituency", "member"])
        w.writerows(rows)
    dates = sorted({r[0] for r in rows})
    print(f"wrote {ATT_CSV.name}: {len(rows)} rows across {len(dates)} sitting day(s)")
    return rows


def report():
    """Per-constituency attendance across all sitting days collected so far."""
    if not ATT_CSV.exists():
        sys.exit("no attendance.csv yet; run `python3 hansard.py run` first")
    with open(ATT_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    dates = sorted({r["sitting_date"] for r in rows})
    seats = {}
    for r in rows:
        s = seats.setdefault(r["constituency"], {"member": r["member"], "present": 0, "absent": 0})
        s[r["status"]] += 1
        if r["member"]:
            s["member"] = r["member"]
    print(f"Sitting days collected: {len(dates)} ({dates[0]} .. {dates[-1]})")
    print(f"Seats seen: {len(seats)}")
    print("\nMost absences (constituency — member: present/total):")
    def total(s):
        return s["present"] + s["absent"]
    ranked = sorted(seats.items(), key=lambda kv: (-kv[1]["absent"], kv[0]))
    for con, s in ranked:
        if s["absent"] == 0:
            continue
        print(f"  {con} — {s['member']}: {s['present']}/{total(s)} present "
              f"({s['absent']} absent)")
    perfect = [c for c, s in seats.items() if s["absent"] == 0]
    print(f"\n{len(perfect)} seat(s) present every collected sitting day.")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "fetch":
        fetch()
    elif cmd == "parse":
        parse_all()
    elif cmd == "run":
        fetch()
        parse_all()
    elif cmd == "report":
        report()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
