#!/usr/bin/env python3
"""Load Malaysian parliamentary replies (Dewan Rakyat / Dewan Negara PDFs) into DuckDB.

    python3 parlimen_replies.py ingest [DIR]   # parse every new PDF in DIR (default ./inbox)
    python3 parlimen_replies.py run            # download new reply PDFs into ./inbox, then ingest
    python3 parlimen_replies.py check          # list replies with no MP, ministry, question or answer

One PDF can hold one reply or many. Each reply becomes one row in the
`replies` table. Files are tracked by sha256 in `files`, so a PDF is
parsed once; pass --force to parse files again after changing TUNE.

If replies are split wrongly or fields come out empty, change the TUNE
patterns below, then run `ingest --force` and `check`.
"""
import argparse
import datetime as dt
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

import duckdb
import pdfplumber
import requests

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "parlimen.duckdb"
INBOX = HERE / "inbox"
SOURCES = HERE / "sources.txt"
CERTS = HERE / "certs"

SITE = "https://www.parlimen.gov.my"
# Pages that link the reply PDFs for the current sitting.
#   jawapan-lisan-dr:      one JDR<ddmmyyyy>.pdf per sitting day, the written
#                          answers to oral questions not reached in the chamber
#   jawapan-bukan-lisan-dr: one JBDR<start><end>.pdf per sitting, the written
#                          questions, posted after the sitting ends
LISTING_PAGES = [
    SITE + "/jawapan-lisan-dr.html?uweb=dr&",
    SITE + "/jawapan-bukan-lisan-dr.html?uweb=dr&",
]
# PDF links on those pages appear as loadResult('/files/...pdf', ...) or href.
PDF_LINK = r"""(?:loadResult\(\s*'|href=["'])([^'"]+?\.pdf)['"]"""

# Regexes, all case-insensitive and multiline. Each runs on the text of one
# PDF after the drop_lines patterns have removed page furniture.
TUNE = {
    # Line that begins a new reply. A file with several replies is cut here.
    "start": r"^\s*PERTANYAAN\s*:\s*(?:LISAN|BUKAN\s+LISAN|BERTULIS)\b.*$",
    # Lines just above "start" that still belong to the reply below
    # (the notice heading and the question number).
    "lead": [
        r"^\s*NO\.?\s*SOALAN\s*:?\s*\d+\s*$",
        r"^\s*PEMBERITAHUAN\s+PERTANYAAN\b.*$",
        r"^\s*MESYUARAT\s+\S+\s*,\s*PENGGAL\b.*$",
        r"^\s*PARLIMEN\s+\S+\s+BELAS\b.*$",
        r"^\s*DEWAN\s+(?:RAKYAT|NEGARA)\s*$",
    ],
    # Running page header repeated on every page of a reply; removed from
    # the question and answer text after the question number is read.
    "running_header": r"^\s*NO\.?\s*SOALAN\s*:?\s*\d+\s*$",
    # Lines removed before parsing: page numbers, running headers.
    "drop_lines": [
        r"^\s*\d{1,3}\s*$",
        r"^\s*(?:muka\s+surat|halaman|page)\s*\d+(?:\s*(?:/|dari|of)\s*\d+)?\s*$",
    ],
    # Multi-line blocks removed before parsing: the cover page listing the
    # questions, which some files repeat part-way through.
    "drop_blocks": [
        r"^\s*JAWAPAN-JAWAPAN\s+BAGI\s+PERTANYAAN[\s\S]*?^\s*NOTA\s*:.*$",
    ],
    # Placeholder page for a question answered together with another one:
    #   NO SOALAN : 22
    #   [SOALAN NO. 22 DIJAWAB BERSEKALI DENGAN SOALAN NO. 2]
    # Group 1 is the question, group 2 the question(s) it was answered with.
    # It becomes a row with answered_with set and no question or answer text.
    "joint": r"^\s*NO\.?\s*SOALAN\s*:?\s*(\d+)\s*\n\s*\[\s*SOALAN\s+NO\.?\s*\1\s+DIJAWAB\s+BERSEKALI\s+DENGAN\s+SOALAN\s+NO\.?\s*([^\]]+?)\s*\]",
    # Header fields inside one reply. Group 1 is the value.
    "kind": r"^\s*PERTANYAAN\s*:\s*(LISAN|BUKAN\s+LISAN|BERTULIS)\b",
    "question_no": r"(?:NO\.?\s*SOALAN|SOALAN\s*NO\.?)\s*:?\s*(\d+)",
    "date": r"^\s*TARIKH\s*:\s*(.+?)\s*$",
    # "DARIPADA : TUAN AHMAD BIN ALI [ KAWASAN ]"  -> MP, constituency.
    # Long names wrap, so one continuation line is allowed.
    "mp": r"^\s*DARIPADA\s*:\s*([^\[\n]+(?:\n(?!\s*(?:TARIKH|SOALAN)\b)[^\[\n:]+)?)\s*(?:\[\s*([^\]]+?)\s*\])?",
    # "... minta MENTERI KEWANGAN menyatakan ..." -> ministry
    "ministry": r"\bminta\s+((?:TIMBALAN\s+)?(?:PERDANA\s+)?MENTERI\b[\s\S]{0,150}?)\s+(?:menyatakan|memberikan|menjelaskan|mengemukakan|merincikan|memaklumkan)\b",
    # Fallback when the minta ... sentence is missing or broken across pages.
    "ministry_fallback": r"^\s*(KEMENTERIAN\s+[A-Z ,&\-]+?)\s*$",
    # Markers that open the question and the answer. Capitals only, so a
    # question that quotes an earlier "jawapan" isn't cut there.
    "question": r"^\s*(?-i:SOALAN)\s*(?:NO\.?\s*\d+\s*)?:?\s*$",
    "answer": r"^\s*(?-i:JAWAPAN)\s*:?(?:\s*$|\s+(?=\S))",
    # Minister named at the top of an answer, before "Tuan Yang di-Pertua"
    # (mostly in answers to the Prime Minister's questions):
    #   YB DATO' SRI AZALINA OTHMAN SAID
    #   MENTERI DI JABATAN PERDANA MENTERI (UNDANG-UNDANG DAN REFORMASI INSTITUSI)
    "answered_by": r"\A\s*((?:YAB|YB)\b[\s\S]{0,300}?)\n\s*(?=Tuan\s+Yang\s+di-?Pertua)",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    sha256      VARCHAR PRIMARY KEY,
    filename    VARCHAR,
    pages       INTEGER,
    text_chars  INTEGER,   -- 0 means a scanned PDF with no text layer
    n_replies   INTEGER,
    ingested_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS replies (
    file_sha256  VARCHAR,
    seq          INTEGER,  -- position of the reply within its file, from 1
    filename     VARCHAR,
    kind         VARCHAR,  -- LISAN / BUKAN LISAN / BERTULIS
    question_no  INTEGER,
    date_text    VARCHAR,  -- as printed; formats vary between sittings
    mp           VARCHAR,
    constituency VARCHAR,
    ministry     VARCHAR,
    question     VARCHAR,
    answer       VARCHAR,
    raw_text     VARCHAR,  -- the whole block, for checking the split
    PRIMARY KEY (file_sha256, seq)
);
-- Question(s) this one was answered together with; question and answer are
-- then empty and the text is under the other question.
ALTER TABLE replies ADD COLUMN IF NOT EXISTS answered_with VARCHAR;
-- Minister named at the top of the answer, when there is one.
ALTER TABLE replies ADD COLUMN IF NOT EXISTS answered_by VARCHAR;
"""

COLUMNS = ["file_sha256", "seq", "filename", "kind", "question_no", "date_text", "mp",
           "constituency", "ministry", "question", "answer", "raw_text", "answered_with", "answered_by"]

FLAGS = re.I | re.M


def clean(s):
    if s is None:
        return None
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s or None


def one_line(s):
    return " ".join(s.split()) if s else None


def pdf_text(path):
    with pdfplumber.open(path) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    text = "\n".join(pages)
    for pat in TUNE["drop_lines"]:
        text = re.sub(pat + r"\n?", "", text, flags=FLAGS)
    for pat in TUNE["drop_blocks"]:
        text = re.sub(pat + r"\n?", "", text, flags=FLAGS)
    return text, len(pages)


def split_replies(text):
    lead = re.compile("|".join(f"(?:{p})" for p in TUNE["lead"]), re.I)
    starts = []
    for m in re.finditer(TUNE["start"], text, FLAGS):
        pos = m.start()
        # Walk back over lead lines so they open this reply, not end the last.
        while pos > 0:
            prev = text.rfind("\n", 0, pos - 1) + 1
            if not lead.fullmatch(text[prev : pos - 1]):
                break
            pos = prev
        starts.append(pos)
    starts = sorted(set(starts) | {m.start() for m in re.finditer(TUNE["joint"], text, FLAGS)})
    if not starts:
        return [text] if text.strip() else []
    return [text[a:b] for a, b in zip(starts, starts[1:] + [len(text)])]


def parse_reply(block):
    def first(key):
        m = re.search(TUNE[key], block, FLAGS)
        return m.group(1) if m else None

    row = {"raw_text": clean(block), "answered_with": None, "answered_by": None}
    joint = re.match(TUNE["joint"], block, FLAGS)
    if joint:
        return dict(row, kind=None, question_no=int(joint.group(1)), date_text=None, mp=None,
                    constituency=None, ministry=None, question=None, answer=None,
                    answered_with=one_line(joint.group(2)))
    kind = first("kind")
    row["kind"] = re.sub(r"\s+", " ", kind.upper()) if kind else None
    qno = first("question_no")
    row["question_no"] = int(qno) if qno else None
    row["date_text"] = clean(first("date"))

    m = re.search(TUNE["mp"], block, FLAGS)
    row["mp"] = one_line(m.group(1)) if m else None
    row["constituency"] = one_line(m.group(2)) if m else None

    ministry = first("ministry") or first("ministry_fallback")
    row["ministry"] = one_line(ministry).upper() if ministry else None

    block = re.sub(TUNE["running_header"] + r"\n?", "", block, flags=FLAGS)

    a = re.search(TUNE["answer"], block, FLAGS)
    q = re.search(TUNE["question"], block, FLAGS)
    if a:
        q_start = q.end() if q and q.end() <= a.start() else None
        if q_start is None:
            # No SOALAN marker: the question is the "... minta ..." paragraph.
            mm = re.search(r"^.*\bminta\b", block[: a.start()], FLAGS)
            q_start = mm.start() if mm else None
        row["question"] = clean(block[q_start : a.start()]) if q_start is not None else None
        answer = block[a.end() :]
        by = re.match(TUNE["answered_by"], answer, re.I)
        if by:
            row["answered_by"] = one_line(by.group(1))
            answer = answer[by.end() :]
        row["answer"] = clean(answer)
    else:
        row["question"] = clean(block[q.end() :]) if q else None
        row["answer"] = None
    return row


def connect():
    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA)
    return con


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest(folder, force=False):
    folder = Path(folder)
    pdfs = sorted(p for p in folder.glob("**/*") if p.suffix.lower() == ".pdf")
    con = connect()
    done = {r[0] for r in con.execute("SELECT sha256 FROM files").fetchall()}
    new = failed = 0
    for path in pdfs:
        digest = sha256(path)
        if digest in done and not force:
            continue
        try:
            text, pages = pdf_text(path)
        except Exception as e:  # a broken PDF should not stop the rest
            print(f"FAILED {path.name}: {e}", file=sys.stderr)
            failed += 1
            continue
        rows = [parse_reply(b) for b in split_replies(text)]
        con.execute("BEGIN")
        con.execute("DELETE FROM replies WHERE file_sha256 = ?", [digest])
        con.execute("DELETE FROM files WHERE sha256 = ?", [digest])
        con.execute(
            "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?)",
            [digest, path.name, pages, len(text.strip()), len(rows), dt.datetime.now()],
        )
        for seq, r in enumerate(rows, 1):
            r = dict(r, file_sha256=digest, seq=seq, filename=path.name)
            con.execute(
                f"INSERT INTO replies ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                [r[c] for c in COLUMNS],
            )
        con.execute("COMMIT")
        new += 1
        note = "  (no text layer: scanned PDF?)" if not text.strip() else ""
        print(f"{path.name}: {len(rows)} replies{note}")
    total = con.execute("SELECT count(*) FROM replies").fetchone()[0]
    print(f"ingested {new} file(s), {failed} failed; replies table has {total} rows")
    con.close()
    return failed


def check():
    con = connect()
    gaps = con.execute("""
        SELECT filename, seq, question_no,
               concat_ws(', ',
                   CASE WHEN mp IS NULL THEN 'mp' END,
                   CASE WHEN ministry IS NULL THEN 'ministry' END,
                   CASE WHEN question IS NULL THEN 'question' END,
                   CASE WHEN answer IS NULL THEN 'answer' END) AS missing
        FROM replies
        WHERE answered_with IS NULL
          AND (mp IS NULL OR ministry IS NULL OR question IS NULL OR answer IS NULL)
        ORDER BY filename, seq
    """).fetchall()
    scanned = con.execute("SELECT filename FROM files WHERE text_chars = 0").fetchall()
    for f, seq, qno, missing in gaps:
        print(f"{f} #{seq} (soalan {qno}): missing {missing}")
    for (f,) in scanned:
        print(f"{f}: no text layer, needs OCR")
    n = con.execute("SELECT count(*) FROM replies").fetchone()[0]
    print(f"{len(gaps)} of {n} replies incomplete, {len(scanned)} scanned file(s)")
    con.close()
    return 1 if gaps or scanned else 0


def ca_bundle():
    """The usual CA bundle plus certs/*.pem.

    www.parlimen.gov.my sends its certificate without the Sectigo
    intermediate, so certs/ holds that intermediate. Verification stays on.
    """
    import certifi

    base = os.environ.get("REQUESTS_CA_BUNDLE") or certifi.where()
    out = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
    out.write(Path(base).read_text())
    for pem in sorted(CERTS.glob("*.pem")):
        out.write("\n" + pem.read_text())
    out.close()
    return out.name


def listed_urls(session, verify):
    """PDF URLs linked from LISTING_PAGES, plus any in sources.txt."""
    urls, failed = [], 0
    for page in LISTING_PAGES:
        try:
            r = session.get(page, timeout=60, verify=verify)
            r.raise_for_status()
        except Exception as e:
            print(f"LISTING FAILED {page}: {e}", file=sys.stderr)
            failed += 1
            continue
        found = [urljoin(page, quote(m, safe="/:%()")) for m in re.findall(PDF_LINK, r.text, re.I)]
        print(f"{page}: {len(found)} PDF link(s)")
        urls += found
    if SOURCES.exists():
        for line in SOURCES.read_text(encoding="utf-8").splitlines():
            url = line.split("#", 1)[0].strip()
            if url:
                urls.append(url)
    return list(dict.fromkeys(urls)), failed


def fetch():
    """Download every listed PDF that isn't already in ./inbox."""
    INBOX.mkdir(exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "parlimen-replies/1.0"
    # Passed per request: requests lets REQUESTS_CA_BUNDLE override session.verify.
    verify = ca_bundle()
    urls, failed = listed_urls(session, verify)
    have = {p.name.lower() for p in INBOX.iterdir()}
    for url in urls:
        name = Path(unquote(urlparse(url).path)).name or hashlib.sha1(url.encode()).hexdigest()
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        if name.lower() in have:
            continue
        dest = INBOX / name
        try:
            r = session.get(url, timeout=120, verify=verify)
            r.raise_for_status()
            if not r.content.startswith(b"%PDF"):
                raise ValueError("response is not a PDF")
        except Exception as e:
            print(f"FETCH FAILED {url}: {e}", file=sys.stderr)
            failed += 1
            continue
        dest.write_bytes(r.content)
        have.add(name.lower())
        print(f"fetched {name}")
    os.unlink(verify)
    return failed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest")
    p.add_argument("folder", nargs="?", default=str(INBOX))
    p.add_argument("--force", action="store_true", help="re-parse files already ingested")
    sub.add_parser("run")
    sub.add_parser("check")
    args = ap.parse_args()

    if args.cmd == "ingest":
        sys.exit(1 if ingest(args.folder, args.force) else 0)
    if args.cmd == "check":
        sys.exit(check())
    if args.cmd == "run":
        print(f"run started {dt.datetime.now():%Y-%m-%d %H:%M}")
        fetch_failed = fetch()
        ingest_failed = ingest(INBOX)
        sys.exit(1 if fetch_failed or ingest_failed else 0)


if __name__ == "__main__":
    main()
