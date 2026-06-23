# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Local FastAPI web app that turns a public YouTube/VK Video URL into an archived,
searchable record: downloaded MP3 audio, an OpenAI Whisper transcript with
timestamps, structured summaries, AI-assigned category, PostgreSQL full-text
search, and pgvector semantic search over OpenAI embeddings. UI strings and AI
prompts are in Italian.

## Commands

Use the local virtualenv at `.venv` (Python 3.11). Do not use `pvenvconf`.

This checkout runs on **Windows**, so the interpreter is `.venv\Scripts\python.exe`
(there is no `.venv/bin/`). The docs below give both forms; on this machine use the
Windows path.

```bash
# Linux/macOS                          # Windows (this checkout)
source .venv/bin/activate              .venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000   # serves UI at http://127.0.0.1:8000
```

Verification (there is no test suite; these are the checks the project uses):

```bash
.venv/bin/python -m compileall app     # Windows: .venv\Scripts\python.exe -m compileall app
node --check static/app.js             # syntax-check frontend
```

Regenerate structured summaries (`summary_short`/`summary_long`/`key_points`) for
existing DB rows that predate those columns:

```bash
.venv/bin/python scripts/backfill_summaries.py   # Windows: .venv\Scripts\python.exe scripts\backfill_summaries.py
```

### Database (PostgreSQL + pgvector)

The backend uses PostgreSQL, not SQLite. The local instance is the Windows
service `postgresql-x64-16` on `localhost:5432` (trust auth for `postgres`, no
password). The app DB is `transcript` with the `vector` extension enabled.
`init_db()` (run on FastAPI startup) creates the schema idempotently.

One-shot data migration from the legacy SQLite file (`data/videos.db`) into
Postgres, generating an embedding per row; idempotent on `url`, safe to re-run:

```bash
.venv\Scripts\python.exe scripts\migrate_sqlite_to_postgres.py
```

psql is at `C:\Program Files\PostgreSQL\16\bin\psql.exe`. Quick check:

```bash
"C:\Program Files\PostgreSQL\16\bin\psql.exe" -h localhost -U postgres -d transcript -w -c "\d videos"
```

### Platform caveat (Linux path hardcoded in code)

`write_pdf` loads `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` (Linux path),
so PDF export still needs that font present — see PDF export caveat below. The old
yt-dlp Linux-path bug is fixed: `run_yt_dlp` now calls `python -m yt_dlp` with the
bundled imageio-ffmpeg binary, so audio download/import works on Windows.

## Configuration

`.env` (loaded from project root via `python-dotenv`; never print or commit it):

- `OPENAI_API_KEY` — required for transcription, summary, categorization, and embeddings.
- `OPENAI_SUMMARY_MODEL` — defaults to `gpt-5.5` (`DEFAULT_SUMMARY_MODEL`); used
  for both summarization and categorization. Transcription is hard-coded to `whisper-1`.
- `OPENAI_EMBEDDING_MODEL` — defaults to `text-embedding-3-small` (1536 dims). If
  you change it, also update `EMBEDDING_DIM` in `app/database.py` and re-embed all
  rows (the `embedding vector(N)` column dimension must match).
- `DATABASE_URL` — defaults to `postgresql://postgres@localhost:5432/transcript`.
- `TS_CONFIG` — Postgres text-search config for the FTS column; defaults to `italian`.

## Architecture

The whole backend is two files plus a static frontend.

- `app/main.py` — all API routes and the processing pipeline. `POST /api/videos`
  runs the full chain synchronously inside one request (no background queue), all
  within a single `TemporaryDirectory`:
  1. `download_metadata` / `download_audio` via `yt-dlp` (invoked through
     `run_yt_dlp` as `python -m yt_dlp`, with `--ffmpeg-location` set to the
     bundled imageio-ffmpeg binary — not as a library, not a system yt-dlp).
  2. `prepare_export_audio` / `convert_audio` — ffmpeg (from bundled
     `imageio-ffmpeg`, not system ffmpeg) re-encodes to **MP3 mono 16 kHz 32 kbps**.
  3. `transcribe_audio_file` — if the file exceeds `OPENAI_AUDIO_LIMIT_BYTES`
     (24 MB), `prepare_audio_chunks` splits it into time segments and each
     chunk's timestamps are shifted by its offset so the merged transcript stays
     globally timed. Whisper returns timestamped segments.
  4. `summarize` — one OpenAI JSON-mode call returns `summary_short`,
     `summary_long`, and `key_points` (each with `time_seconds`/`title`/`detail`).
     `normalize_summary_data` defends against malformed model output.
  5. `categorize_video` — separate OpenAI call for a short Italian category, with
     `fallback_category` from source metadata if the call fails.
  6. `embed_text` (`build_embedding_text`) — one OpenAI embeddings call over
     title + summary + transcript (truncated to `EMBEDDING_INPUT_CHARS`), stored
     in the `embedding` pgvector column.
  7. `persist_audio` copies the MP3 into `data/audio/`; `save_video` writes the row
     (`INSERT ... RETURNING id`).

  `GET /api/videos` supports two search modes via `?mode=`: `keyword` (default,
  Postgres FTS over `search_vector`) and `semantic` (pgvector cosine distance,
  `embedding <=> %s::vector`, ordering by nearest query embedding).

- `app/database.py` — PostgreSQL (`DATABASE_URL`, default DB `transcript`) via
  `psycopg` 3 with `dict_row`; `register_vector` enables pgvector on each
  connection. Single `videos` table with: a `search_vector` **generated** tsvector
  column (`TS_CONFIG`, GIN index) replacing the old SQLite FTS5 table + triggers;
  and an `embedding vector(EMBEDDING_DIM)` column with an IVFFlat cosine index.
  Schema migrations are idempotent via `ensure_column` (`ADD COLUMN IF NOT EXISTS`).
  Query placeholders are `%s` (not `?`).

- `static/` — vanilla JS frontend (`app.js`, `index.html`, `styles.css`); the
  detail modal embeds an audio player and key points seek the player by
  `time_seconds`. Mounted at `/static`.

### Key design rules

- Always download and transcribe with OpenAI Whisper. Do **not** use YouTube/VK
  subtitles as the transcript source. (`parse_vtt`/caption helpers exist but the
  pipeline does not feed captions into the transcript.)
- Timestamp offsets must be preserved across chunk boundaries — anything touching
  chunking must keep `add_offset` semantics intact.
- `key_points` timestamps drive frontend audio seeking; keep the
  `time_seconds`/`title`/`detail` shape stable across backend, DB, and `app.js`.

## Data

Video records live in PostgreSQL (DB `transcript`). Runtime files under `data/`
are local and git-ignored: the legacy `data/videos.db` (SQLite, now only a
migration source), `data/audio/` MP3s, and generated export PDFs.

## PDF export caveat

`write_pdf` loads `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` (Linux path)
for Unicode output; PDF export requires that font to be present.
