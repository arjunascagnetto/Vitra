# Memory

## User Preferences

- Use a web GUI.
- Use Python 3.11 in a local `.venv`.
- Do not use `pvenvconf`.
- Read API keys from `.env`.
- Always download audio and process it through OpenAI transcription.
- Do not rely on platform subtitles as the transcript source.
- Categories must be automatic and generated through OpenAI.
- Main page should show only smaller video posters, grouped by category.
- Clicking a poster opens the video detail modal.
- Summary output should not be only bullet points:
  - short discursive summary
  - long discursive summary
  - table of important timestamped points
- Timestamped points must be clickable and play the corresponding audio position.
- Audio must be downloadable from the video detail view.

## Current State

- Virtualenv exists at `.venv`.
- `.env` exists locally and is read by the app, but is not tracked.
- Server has been run locally at `http://127.0.0.1:8000`.
- Existing DB rows were backfilled with structured summaries.
- Existing processed videos have MP3 audio saved and linked in SQLite.

## Operational Notes

- Restart Uvicorn after changing `.env`, backend code, or routes.
- `yt-dlp` is pinned to a newer version because older versions failed on YouTube extraction.
- `imageio-ffmpeg` is used so the app does not depend on a system `ffmpeg` binary.
- Audio files are stored as compressed MP3 for both transcription and download.
