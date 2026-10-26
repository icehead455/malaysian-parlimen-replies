#!/usr/bin/env python3
"""Load Malaysian parliamentary replies (Dewan Rakyat / Dewan Negara PDFs) into DuckDB.

    python3 parlimen_replies.py ingest [DIR]   # parse every new PDF in DIR (default ./inbox)
    python3 parlimen_replies.py run            # fetch the URLs in sources.txt into ./inbox, then ingest
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
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import duckdb
import pdfplumber
import requests

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "parlimen.duckdb"
INBOX = HERE / "inbox"
SOURCES = HERE / "sources.txt"

# Regexes, all case-insensitive and multiline. Each runs on the text of one
# PDF after the drop_lines patterns have removed page furniture.
TUNE = {
    # Line that begins a new reply. A file with several replies is cut here.
    "start": r"^\s*PERTANYAAN\s*:\s*(?:LISAN|BUKAN\s+LISAN|BERTULIS)\b.*$",
    # Lines removed before parsing: page numbers, running headers.
    "drop_lines": [
        r"^\s*\d{1,3}\s*$",
        r"^\s*(?:muka\s+surat|halaman|page)\s*\d+(?:\s*(?:/|dari|of)\s*\d+)?\s*$",
    ],
    # Header fields inside one reply. Group 1 is the value.
    "kind": r"^\s*PERTANYAAN\s*:\s*(LISAN|BUKAN\s+LISAN|BERTULIS)\b",
    "question_no": r"(?:NO\.?\s*SOALAN|SOALAN\s*NO\.?)\s*:?\s*(\d+)",
    "date": r"^\s*TARIKH\s*:\s*(.+?)\s*$",
    # "DARIPADA : TUAN AHMAD BIN ALI [ KAWASAN ]"  -> MP, constituency
    "mp": r"^\s*DARIPADA\s*:\s*(.+?)\s*(?:\[\s*([^\]]+?)\s*\])?\s*$",
    # "... minta MENTERI KEWANGAN menyatakan ..." -> ministry
    "ministry": r"\bminta\s+((?:TIMBALAN\s+)?(?:PERDANA\s+)?MENTERI\b[\s\S]{0,150}?)\s+(?:menyatakan|memberikan|menjelaskan|mengemukakan|merincikan|memaklumkan)\b",
    # Fallback when the minta ... sentence is missing or broken across pages.
    "ministry_fallback": r"^\s*(KEMENTERIAN\s+[A-Z ,&\-]+?)\s*$",
    # Markers that open the question and the answer.
    "question": r"^\s*SOALAN\s*(?:NO\.?\s*\d+\s*)?:?\s*$",
    "answer": r"^\s*JAWAPAN\s*:?",
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
"""

FLAGS = re.I | re.M


def clean(s):
    if s is None:
        return None
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s or None


def pdf_text(path):
    with pdfplumber.open(path) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    text = "\n".join(pages)
    for pat in TUNE["drop_lines"]:
        text = re.sub(pat + r"\n?", "", text, flags=FLAGS)
    return text, len(pages)


def split_replies(text):
    starts = [m.start() for m in re.finditer(TUNE["start"], text, FLAGS)]
    if not starts:
        return [text] if text.strip() else []
    return [text[a:b] for a, b in zip(starts, starts[1:] + [len(text)])]


def parse_reply(block):
    def first(key):
        m = re.search(TUNE[key], block, FLAGS)
        return m.group(1) if m else None

    row = {"raw_text": clean(block)}
    kind = first("kind")
    row["kind"] = re.sub(r"\s+", " ", kind.upper()) if kind else None
    qno = first("question_no")
    row["question_no"] = int(qno) if qno else None
    row["date_text"] = clean(first("date"))

    m = re.search(TUNE["mp"], block, FLAGS)
    row["mp"] = clean(m.group(1)) if m else None
    row["constituency"] = clean(m.group(2)) if m and m.lastindex >= 2 else None

    ministry = first("ministry") or first("ministry_fallback")
    row["ministry"] = clean(re.sub(r"\s+", " ", ministry).upper()) if ministry else None

    a = re.search(TUNE["answer"], block, FLAGS)
    q = re.search(TUNE["question"], block, FLAGS)
    if a:
        q_start = q.end() if q and q.end() <= a.start() else None
        if q_start is None:
            # No SOALAN marker: the question is the "... minta ..." paragraph.
            mm = re.search(r"^.*\bminta\b", block[: a.start()], FLAGS)
            q_start = mm.start() if mm else None
        row["question"] = clean(block[q_start : a.start()]) if q_start is not None else None
        row["answer"] = clean(block[a.end() :])
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
            con.execute(
                "INSERT INTO replies VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [digest, seq, path.name, r["kind"], r["question_no"], r["date_text"],
                 r["mp"], r["constituency"], r["ministry"], r["question"], r["answer"],
                 r["raw_text"]],
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
        WHERE mp IS NULL OR ministry IS NULL OR question IS NULL OR answer IS NULL
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


def fetch():
    """Download every URL in sources.txt that isn't already in ./inbox."""
    if not SOURCES.exists():
        return 0
    INBOX.mkdir(exist_ok=True)
    failed = 0
    for line in SOURCES.read_text(encoding="utf-8").splitlines():
        url = line.split("#", 1)[0].strip()
        if not url:
            continue
        name = Path(unquote(urlparse(url).path)).name or hashlib.sha1(url.encode()).hexdigest()
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        dest = INBOX / name
        if dest.exists():
            continue
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            if not r.content.startswith(b"%PDF"):
                raise ValueError("response is not a PDF")
        except Exception as e:
            print(f"FETCH FAILED {url}: {e}", file=sys.stderr)
            failed += 1
            continue
        dest.write_bytes(r.content)
        print(f"fetched {name}")
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
