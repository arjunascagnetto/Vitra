# AGENTS.md

## Project

Local FastAPI web app for processing YouTube/VK Video URLs into:

- downloaded MP3 audio
- OpenAI Whisper transcript with timestamps
- short discursive summary
- long discursive summary
- timestamped important points linked to the local audio player
- SQLite archive with category grouping and full text search

## Runtime

- Use the local virtualenv at `.venv`.
- Python target: 3.11.
- Do not use `pvenvconf`.
- The app reads `.env` from the project root via `python-dotenv`.
- Do not print or commit `.env`.

## Commands

```bash
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Verification:

```bash
.venv/bin/python -m compileall app
node --check static/app.js
```

Backfill structured summaries for existing DB rows:

```bash
.venv/bin/python scripts/backfill_summaries.py
```

## Data

Runtime data is local and intentionally not tracked:

- `data/videos.db`
- `data/audio/`
- generated PDFs

## Implementation Notes

- Always download audio and transcribe with OpenAI; do not use YouTube/VK subtitles as the transcript source.
- Audio is converted to MP3 mono 16 kHz at 32 kbps before transcription and storage.
- If audio still exceeds OpenAI upload limits, it is split into chunks while preserving timestamp offsets.
- Summary generation returns structured JSON: `summary_short`, `summary_long`, `key_points`.
- Each key point contains `time_seconds`, `title`, and `detail`.
- The frontend uses the key point timestamp to seek the local audio player.
