# malaysian-parlimen-replies

Downloads Dewan Rakyat reply PDFs from www.parlimen.gov.my and loads them
into DuckDB, one row per question.

```
pip install -r requirements.txt
python3 parlimen_replies.py run        # download new PDFs into inbox/, then ingest
python3 parlimen_replies.py ingest     # parse new PDFs in inbox/ (--force to redo all)
python3 parlimen_replies.py check      # list replies missing MP, ministry, question or answer
python3 -m pytest -q tests
```

## What gets downloaded

`run` reads the PDF links on the two pages in `LISTING_PAGES`, plus any URLs
in `sources.txt`, and downloads those not already in `inbox/`:

- **Jawapan Lisan** (`JDR<ddmmyyyy>.pdf`): one file per sitting day with the
  written answers to oral questions not reached in the chamber. Dewan Rakyat
  sits Monday to Thursday.
- **Jawapan Bukan Lisan** (`JBDR<start><end>.pdf`): one file per meeting with
  the written questions, posted after the meeting ends.

The site lists only the current meeting, so the PDFs are committed in
`inbox/` to keep older ones.

`www.parlimen.gov.my` sends its certificate without the issuing intermediate,
so `certs/` holds that intermediate (Sectigo Public Server Authentication CA
DV R36, which chains to the Sectigo R46 root). Downloads are verified
against the usual CA bundle plus that file.

## Tables

The database is `parlimen.duckdb` (gitignored; rebuilt from `inbox/`).

- `replies`: `filename`, `seq`, `kind` (LISAN / BUKAN LISAN), `question_no`,
  `date_text`, `mp`, `constituency`, `ministry` (as addressed in the question),
  `answered_by` (minister named at the top of the answer, mostly for
  questions to the Prime Minister), `question`, `answer`, `answered_with`
  (set when the question was answered together with another one; the text
  is then under that question), and `raw_text` (the whole block, for
  checking the split).
- `files`: one row per PDF, with `text_chars = 0` for scanned PDFs that
  need OCR.

## Tuning

The regexes are in `TUNE` at the top of `parlimen_replies.py`. A file is cut
into replies at each `PERTANYAAN : LISAN/BUKAN LISAN` line, together with the
`NO SOALAN` and notice lines just above it. The MP comes from the
`DARIPADA :` line, the ministry from "minta MENTERI ... menyatakan", and the
answer from everything after the `JAWAPAN` line. Cover pages, page numbers
and the `NO SOALAN` header repeated on every page are removed. After editing
a pattern:

```
python3 parlimen_replies.py ingest --force
python3 parlimen_replies.py check
```

Not every number in a file's cover range has a reply: questions answered in
the chamber are left out, and ones answered together with another question
appear as `answered_with` rows.

Test PDFs in `tests/` are generated on the fly and every name in them is
made up.
