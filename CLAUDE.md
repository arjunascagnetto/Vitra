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
- `TAVILY_API_KEY` — optional; enables the chat's `web_search` tool (`TAVILY_MAX_RESULTS`
  caps results). Absent → chat is video-only.
- Local transcription (faster-whisper): `WHISPER_MODEL` (`large-v3`), `WHISPER_DEVICE`
  (`auto`), `WHISPER_COMPUTE_TYPE` (`default`). GPU works via **torch (CUDA build,
  cu124)** — `get_local_whisper_model` imports `torch` first so CTranslate2 finds
  torch's bundled cuBLAS/cuDNN. Do **not** add the `nvidia-cublas/cudnn` DLL wheels;
  torch provides them. Install torch from the CUDA index, e.g.
  `pip install torch --index-url https://download.pytorch.org/whl/cu124`.
- Cost-estimate prices (USD, real OpenAI rates, override if they change):
  `WHISPER_USD_PER_MINUTE` (0.006), `SUMMARY_USD_PER_1M_INPUT` (2.50),
  `SUMMARY_USD_PER_1M_OUTPUT` (15.0), `EMBEDDING_USD_PER_1M` (0.02). Token-billed
  calls are sized from duration via `TRANSCRIPT_TOKENS_PER_MINUTE`/`SUMMARY_OUTPUT_TOKENS`.

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
  3. `transcribe_audio_file` — backend selectable per request via the
     `transcription_backend` form field. `"openai"` (default) uses the Whisper API:
     if the file exceeds `OPENAI_AUDIO_LIMIT_BYTES` (24 MB), `prepare_audio_chunks`
     splits it and each chunk's timestamps are shifted by its offset so the merged
     transcript stays globally timed. `"local"` runs faster-whisper
     (`transcribe_audio_local`, model cached in `get_local_whisper_model`) on this
     machine — free, no size limit, no chunking. Both return the same
     `{start, end, text}` segments.
  3b. `translate_segments` — when the source language isn't Italian (explicit
     `ru` hint or Whisper-detected), translate the transcript **per segment** to
     Italian (batched `gpt-5.4` JSON calls, 1:1 index mapping with fallback to the
     original), stored in `translation_json`. Italian sources get no translation.
  4. `summarize` — one OpenAI JSON-mode call returns `summary_short`,
     `summary_long`, and `key_points` (each with `time_seconds`/`title`/`detail`).
     Summaries are **always in Italian** regardless of source language;
     `normalize_summary_data` defends against malformed model output.
  5. `categorize_video` — separate OpenAI call for a short Italian category, with
     `fallback_category` from source metadata if the call fails.
  6. `embed_text` (`build_embedding_text`) — one OpenAI embeddings call over
     title + summary + transcript (truncated to `EMBEDDING_INPUT_CHARS`), stored
     in the `embedding` pgvector column.
  7. `persist_audio` copies the MP3 into `data/audio/`; `save_video` writes the row
     (`INSERT ... RETURNING id`). `audio_path` is stored **relative to the project
     root** (POSIX separators) via `store_audio_path`, so the DB is portable across
     machines; `resolve_audio_path` resolves it back (relative→`BASE_DIR`, absolute
     for legacy rows, else falls back to `data/audio/<audio_filename>`).

  `GET /api/videos` supports two search modes via `?mode=`: `keyword` (default,
  Postgres FTS over `search_vector`) and `semantic` (pgvector cosine distance,
  `embedding <=> %s::vector`, ordering by nearest query embedding).
  `DELETE /api/videos/{id}` removes the row and best-effort unlinks its MP3.
  `GET`/`POST /api/videos/{id}/chat` is a persisted, transcript-grounded chat
  (`chat_with_video`, cloud `gpt-5.4`, answers in Italian); messages live in the
  `video_messages` table (cascade-deleted with the video). When `TAVILY_API_KEY` is
  set the chat gets a `web_search` tool (`tavily_search`) via a tool-calling loop
  (max `CHAT_MAX_TOOL_ITERATIONS`), used only when the answer isn't in the video;
  without the key the chat stays video-only. The chat sends the **full** history each
  turn; `DELETE /api/videos/{id}/chat` resets it and `POST .../chat/compact`
  (`summarize_conversation`) replaces it with a single recap message — surfaced as
  "Compatta"/"Reset" buttons under the chat.
  A separate **corpus-wide chat** (`GET/POST/DELETE /api/chat`, `general_chat`,
  persisted in `general_messages`) answers across all videos: the system prompt holds a
  catalog (id/category/short+long summaries) of the in-scope videos, and the model reads
  full transcripts (`get_transcript`) and per-video chat histories (`get_video_chat`,
  scoped to in-scope videos) on demand, plus `web_search`. The
  "Chat archivio" dialog has per-category multi-select toggles that scope which videos
  are included (none selected = all); the selected category names are sent as `categories`. `language_hint` is now
  the **source/transcription language only**; export adds a `translation` kind.
  Categories are first-class: `GET/POST /api/categories`, `DELETE /api/categories/{name}`
  (videos fall back to the `UNCATEGORIZED` sentinel, not deleted), and
  `PUT /api/videos/{id}/category` (auto-registers a new name). `videos.category` stays
  the denormalized per-video value; the `categories` table is the canonical registry.
  `POST /api/videos/estimate` returns a per-stage USD cost breakdown
  (transcription/summary/embedding + total) from `estimate_costs`, computed from
  the source duration *before* processing; with `transcription_backend=local` the
  transcription cost is $0. The total is stored in `estimated_cost_usd` on
  `save_video`. The frontend opens a settings dialog (`openSettings`, model backend
  per phase + live cost) on "Processa" and requires confirmation before calling
  `POST /api/videos`. Summary/embedding local backends are not wired yet (the
  dialog's local options for them are disabled).

- `app/database.py` — PostgreSQL (`DATABASE_URL`, default DB `transcript`) via
  `psycopg` 3 with `dict_row`; `register_vector` enables pgvector on each
  connection. Single `videos` table with: a `search_vector` **generated** tsvector
  column (`TS_CONFIG`, GIN index) replacing the old SQLite FTS5 table + triggers;
  and an `embedding vector(EMBEDDING_DIM)` column with an IVFFlat cosine index;
  `translation_json` holds the per-segment Italian translation. A separate
  `video_messages` table (FK to `videos`, `ON DELETE CASCADE`) stores per-video chat,
  and a `categories` table (seeded from existing video categories) is the canonical
  category registry.
  Schema migrations are idempotent via `ensure_column` (`ADD COLUMN IF NOT EXISTS`).
  Query placeholders are `%s` (not `?`).

- `static/` — vanilla JS frontend (`app.js`, `index.html`, `styles.css`); the
  detail modal embeds an audio player and key points seek the player by
  `time_seconds`. Posters carry a trash button (`deleteVideo`); during import an
  indeterminate progress bar advances timed stage messages (`PROCESSING_STAGES`)
  since the backend processes synchronously and reports no real progress.
  `openConfirm()` is a themed, promise-based `<dialog>` (replaces native
  `window.confirm`) used for the cost-estimate gate before processing and for the
  delete confirmation (danger variant) — keep both flows on it, not on
  `window.confirm`. The detail modal also has a **Traduzione** tab (shown only when
  `video.translation` exists; original ↔ Italian per segment) and a **Chat** tab
  (`setupChat`, interactive — does its own DOM updates, not a full `renderDetail`).
  The detail category pill is a button opening a picker (assign existing or create-new
  → `assignCategory`); a toolbar "Categorie" button opens a manager (create/delete →
  `openCategoriesManager`). Mounted at `/static`.

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
migration source), `data/audio/` MP3s, and generated export PDFs. Paths stored in
the DB (`audio_path`) are kept **relative to the project root** — don't write
absolute paths back.

## PDF export caveat

`write_pdf` loads `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` (Linux path)
for Unicode output; PDF export requires that font to be present.
