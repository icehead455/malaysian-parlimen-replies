"""Tests on synthetic PDFs. Every name, constituency and figure here is made up."""
import sys
from pathlib import Path

import duckdb
import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import parlimen_replies as pr  # noqa: E402

COVER = """JAWAPAN-JAWAPAN BAGI PERTANYAAN-PERTANYAAN JAWAB
LISAN YANG TIDAK DIJAWAB DI DALAM DEWAN
( SOALAN NO. 7 HINGGA NO. 9 )
[SOALAN NO. 8 DIJAWAB BERSEKALI DENGAN SOALAN NO. 7]
NOTA: [RUJUK PENYATA RASMI HARIAN (HANSARD)]"""

# Layout of the daily JDR files: question number above the notice heading,
# repeated at the top of every page.
REPLY_A = """NO SOALAN : 12
PEMBERITAHUAN PERTANYAAN DEWAN RAKYAT
MESYUARAT KETIGA, PENGGAL KELIMA
PARLIMEN KELIMA BELAS TAHUN 2026
PERTANYAAN : BUKAN LISAN
DARIPADA : TUAN CONTOH SATU BIN CONTOH
PANJANG [ KAWASAN A ]
TARIKH : 04.03.2024
SOALAN
TUAN CONTOH SATU [ KAWASAN A ] minta MENTERI KEWANGAN menyatakan
jumlah peruntukan susulan
jawapan Kementerian pada tahun lalu.
<PAGE>
NO SOALAN : 12
JAWAPAN
Tuan Yang di-Pertua,
Peruntukan ialah RM1 juta.
<PAGE>
NO SOALAN : 12
Sekian, terima kasih."""

REPLY_B = """NO SOALAN : 7
PERTANYAAN : LISAN
DARIPADA : PUAN CONTOH DUA [ KAWASAN B ]
TARIKH : 05.03.2024
Puan Contoh Dua [ Kawasan B ] minta MENTERI PERDAGANGAN
ANTARABANGSA DAN INDUSTRI menyatakan status eksport.
JAWAPAN :
Eksport meningkat."""

JOINT = """NO SOALAN : 8
[SOALAN NO. 8 DIJAWAB BERSEKALI DENGAN SOALAN NO. 7]
NOTA: [BOLEH RUJUK PENYATA RASMI HARIAN UNTUK JAWAPAN]"""

REPLY_C = """NO SOALAN : 9
PERTANYAAN : BUKAN LISAN
DARIPADA : TUAN CONTOH TIGA [ KAWASAN C ]
SOALAN
Tuan Contoh Tiga minta PERDANA MENTERI menyatakan dasar baharu.
JAWAPAN
YB CONTOH EMPAT
MENTERI DI JABATAN PERDANA MENTERI
(HAL EHWAL CONTOH)
Tuan Yang di-Pertua,
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
    # cover page repeated part-way through, as some JDR files do
    make_pdf(inbox / "multi.pdf", "\n<PAGE>\n".join([COVER, REPLY_B, JOINT, COVER, REPLY_C]))
    assert pr.ingest(inbox) == 0
    con = duckdb.connect(str(tmp_path / "t.duckdb"))
    yield con, inbox
    con.close()


def rows(con):
    cols = ("filename, seq, kind, question_no, mp, constituency, ministry, question, answer, "
            "answered_with, answered_by")
    q = f"SELECT {cols} FROM replies ORDER BY filename, seq"
    return [dict(zip(cols.split(", "), r)) for r in con.execute(q).fetchall()]


def test_split_and_fields(db):
    con, _ = db
    r = rows(con)
    assert [(x["filename"], x["question_no"]) for x in r] == [
        ("multi.pdf", 7), ("multi.pdf", 8), ("multi.pdf", 9), ("single.pdf", 12)]
    b, joint, c, a = r
    assert a["kind"] == "BUKAN LISAN"
    # name wraps onto a second line
    assert a["mp"] == "TUAN CONTOH SATU BIN CONTOH PANJANG" and a["constituency"] == "KAWASAN A"
    assert a["ministry"] == "MENTERI KEWANGAN"
    # a lower-case "jawapan" inside the question is not the answer marker
    assert a["question"].startswith("TUAN CONTOH SATU") and a["question"].endswith("tahun lalu.")
    # answer runs across pages; page numbers and repeated page headers are gone
    assert a["answer"] == "Tuan Yang di-Pertua,\nPeruntukan ialah RM1 juta.\nSekian, terima kasih."
    assert b["kind"] == "LISAN"
    assert b["ministry"] == "MENTERI PERDAGANGAN ANTARABANGSA DAN INDUSTRI"
    assert b["question"].startswith("Puan Contoh Dua")  # no SOALAN marker
    assert b["answer"] == "Eksport meningkat."  # the cover page after it is dropped
    assert joint["answered_with"] == "7" and joint["answer"] is None and joint["mp"] is None
    assert c["ministry"] == "PERDANA MENTERI"
    assert c["answered_by"] == "YB CONTOH EMPAT MENTERI DI JABATAN PERDANA MENTERI (HAL EHWAL CONTOH)"
    assert c["answer"] == "Tuan Yang di-Pertua,\nDasar sedang dikaji."


def test_reingest_is_idempotent(db):
    con, inbox = db
    con.close()
    pr.ingest(inbox)
    pr.ingest(inbox, force=True)
    con = duckdb.connect(str(pr.DB_PATH))
    assert con.execute("SELECT count(*) FROM replies").fetchone()[0] == 4
    assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 2
    con.close()


def test_check_passes_on_complete_replies(db, capsys):
    con, _ = db
    con.close()
    assert pr.check() == 0
