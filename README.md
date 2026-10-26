# malaysian-parlimen-replies

Turns Malaysian parliamentary reply PDFs (Dewan Rakyat / Dewan Negara,
oral and written) into a DuckDB table, one row per reply.

```
pip install -r requirements.txt
python3 parlimen_replies.py ingest     # parse new PDFs in inbox/
python3 parlimen_replies.py run        # download URLs in sources.txt to inbox/, then ingest
python3 parlimen_replies.py check      # list replies missing MP, ministry, question or answer
python3 -m pytest -q tests
```

The database is `parlimen.duckdb` (gitignored; it can always be rebuilt
from `inbox/`). Tables:

- `replies`: `filename`, `seq`, `kind` (LISAN / BUKAN LISAN), `question_no`,
  `date_text`, `mp`, `constituency`, `ministry`, `question`, `answer`, and
  `raw_text` (the whole block, for checking how the file was split).
- `files`: one row per PDF, with `text_chars = 0` for scanned PDFs that
  need OCR.

## Tuning

The regexes are in `TUNE` at the top of `parlimen_replies.py`. A file is
cut into replies at every line matching `TUNE["start"]`
(`PERTANYAAN : LISAN/BUKAN LISAN`). The MP comes from the `DARIPADA :` line,
the ministry from "minta MENTERI ... menyatakan", and the answer from
everything after `JAWAPAN`. After editing a pattern:

```
python3 parlimen_replies.py ingest --force
python3 parlimen_replies.py check
```

Test PDFs in `tests/` are generated on the fly and every name in them is
made up.
