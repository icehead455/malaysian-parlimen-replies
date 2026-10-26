"""Tests on synthetic PDFs. Every name, constituency and figure here is made up."""
import sys
from pathlib import Path

import duckdb
import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import parlimen_replies as pr  # noqa: E402

REPLY_A = """DEWAN RAKYAT
PERTANYAAN : BUKAN LISAN
DARIPADA : TUAN CONTOH SATU [ KAWASAN A ]
TARIKH : 04.03.2024
NO. SOALAN : 12
SOALAN
Tuan Contoh Satu [ Kawasan A ] minta MENTERI KEWANGAN menyatakan
jumlah peruntukan bagi tahun 2024.
JAWAPAN
Tuan Yang di-Pertua,
Peruntukan ialah RM1 juta.
<PAGE>
Sekian, terima kasih."""

REPLY_B = """PERTANYAAN : LISAN
DARIPADA : PUAN CONTOH DUA [ KAWASAN B ]
TARIKH : 05.03.2024
NO. SOALAN : 7
Puan Contoh Dua [ Kawasan B ] minta MENTERI PERDAGANGAN
ANTARABANGSA DAN INDUSTRI menyatakan status eksport.
JAWAPAN :
Eksport meningkat."""

REPLY_C = """PERTANYAAN : BUKAN LISAN
DARIPADA : TUAN CONTOH TIGA [ KAWASAN C ]
SOALAN
Tuan Contoh Tiga minta PERDANA MENTERI menyatakan dasar baharu.
JAWAPAN
Dasar sedang dikaji."""


def make_pdf(path, text):
    c = canvas.Canvas(str(path), pagesize=A4)
    page = 1
    for chunk in text.split("<PAGE>"):
        y = 800
        for line in chunk.strip("\n").splitlines():
            c.drawString(50, y, line)
            y -= 16
        c.drawString(290, 30, str(page))  # page number, dropped by drop_lines
        c.showPage()
        page += 1
    c.save()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "DB_PATH", tmp_path / "t.duckdb")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    make_pdf(inbox / "single.pdf", REPLY_A)
    make_pdf(inbox / "multi.pdf", REPLY_B + "\n<PAGE>\n" + REPLY_C)
    assert pr.ingest(inbox) == 0
    con = duckdb.connect(str(tmp_path / "t.duckdb"))
    yield con, inbox
    con.close()


def rows(con):
    cols = "filename, seq, kind, question_no, mp, constituency, ministry, question, answer"
    q = f"SELECT {cols} FROM replies ORDER BY filename, seq"
    return [dict(zip(cols.split(", "), r)) for r in con.execute(q).fetchall()]


def test_split_and_fields(db):
    con, _ = db
    r = rows(con)
    assert [(x["filename"], x["seq"]) for x in r] == [("multi.pdf", 1), ("multi.pdf", 2), ("single.pdf", 1)]
    b, c, a = r
    assert a["kind"] == "BUKAN LISAN" and a["question_no"] == 12
    assert a["mp"] == "TUAN CONTOH SATU" and a["constituency"] == "KAWASAN A"
    assert a["ministry"] == "MENTERI KEWANGAN"
    assert a["question"].startswith("Tuan Contoh Satu") and "JAWAPAN" not in a["question"]
    # answer runs across the page break and the page number is gone
    assert a["answer"].endswith("Sekian, terima kasih.") and "\n1\n" not in a["answer"]
    assert b["kind"] == "LISAN"
    assert b["ministry"] == "MENTERI PERDAGANGAN ANTARABANGSA DAN INDUSTRI"
    assert b["question"].startswith("Puan Contoh Dua")  # no SOALAN marker
    assert b["answer"] == "Eksport meningkat."
    assert c["ministry"] == "PERDANA MENTERI"
    assert c["answer"] == "Dasar sedang dikaji."


def test_reingest_is_idempotent(db):
    con, inbox = db
    con.close()
    pr.ingest(inbox)
    pr.ingest(inbox, force=True)
    con = duckdb.connect(str(pr.DB_PATH))
    assert con.execute("SELECT count(*) FROM replies").fetchone()[0] == 3
    assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 2
    con.close()


def test_check_passes_on_complete_replies(db, capsys):
    con, _ = db
    con.close()
    assert pr.check() == 0
