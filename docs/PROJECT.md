# Project Notes

## Purpose

This app is a local web GUI for archiving and analyzing YouTube/VK Video content. The user submits a public video URL, the app downloads the audio, transcribes it, summarizes it, categorizes it, stores everything in SQLite, and exposes exports from the browser.

## Current Features

- FastAPI backend with static frontend.
- SQLite storage with FTS5 search.
- Automatic category generation through OpenAI.
- Audio download through `yt-dlp`.
- Audio conversion/chunking through bundled `imageio-ffmpeg`.
- OpenAI audio transcription with timestamped segments.
- Structured OpenAI summary:
  - brief discursive summary
  - long discursive summary
  - important timestamped points
- Modal detail view with audio player and seekable key points.
- TXT/PDF exports for summary and transcript.
- JSON export for timestamped transcript.
- MP3 audio download.

## Main Files

- `app/main.py`: API routes, video processing, OpenAI calls, exports.
- `app/database.py`: SQLite setup and migrations.
- `static/app.js`: frontend behavior.
- `static/styles.css`: frontend layout and detail modal.
- `scripts/backfill_summaries.py`: regenerates structured summaries for existing DB rows.
- `requirements.txt`: pinned Python dependencies.

## Local Data

The application creates local runtime data under `data/`.

- `data/videos.db`: SQLite database.
- `data/audio/`: processed MP3 audio files.

These files are ignored by Git.
