# Video Transcript GUI

Web GUI locale per scaricare audio da link YouTube/VK Video, generare trascrizioni
con timestamp, creare riassunti strutturati, categorizzare e archiviare ogni video
in **PostgreSQL** con ricerca full-text e ricerca vettoriale su embedding via **pgvector**.

## Requisiti

- Python 3.11
- **PostgreSQL** (16) con l'estensione **pgvector** disponibile
- `OPENAI_API_KEY` configurata in `.env` (trascrizione, riassunto, categoria, embedding)

ffmpeg e yt-dlp non vanno installati a parte: ffmpeg è incluso via `imageio-ffmpeg`
e yt-dlp è una dipendenza Python invocata come modulo (`python -m yt_dlp`).

## Installazione rapida (auto installer)

Assicurati che PostgreSQL sia in esecuzione, poi lancia l'installer dalla root del
progetto. Crea il virtualenv `.venv`, installa le dipendenze, genera `.env` da
`.env.example` e configura il database (crea il DB, abilita pgvector, crea lo schema).

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

Linux/macOS:

```bash
bash install.sh
```

Al termine:

1. Apri `.env` e inserisci la tua `OPENAI_API_KEY` (e, se serve, modifica
   `DATABASE_URL`).
2. Avvia l'app:

   ```powershell
   # Windows
   .venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
   ```

   ```bash
   # Linux/macOS
   .venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
   ```

3. Apri `http://127.0.0.1:8000`.

L'installer è idempotente: puoi rilanciarlo senza rischi (non sovrascrive un `.env`
esistente né ricrea il database se è già presente).

## Configurazione manuale

Se preferisci non usare l'installer:

```sql
CREATE DATABASE transcript;
\c transcript
CREATE EXTENSION IF NOT EXISTS vector;
```

Configura `.env` (vedi `.env.example`):

```bash
OPENAI_API_KEY="..."
OPENAI_SUMMARY_MODEL=gpt-5.5
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
DATABASE_URL=postgresql://postgres@localhost:5432/transcript
```

Installa le dipendenze e configura DB + schema (oppure lascia che `init_db()` crei
lo schema al primo avvio):

```bash
.venv/bin/python -m pip install -r requirements.txt   # Windows: .venv\Scripts\python.exe ...
.venv/bin/python scripts/setup_db.py                  # crea DB, estensione e schema
```

Avvia con `uvicorn app.main:app --reload --host 127.0.0.1 --port 8000` e apri
`http://127.0.0.1:8000`.

## Funzioni

- Import da YouTube o VK Video tramite URL pubblico.
- Categorie automatiche per separare i video.
- Ricerca full-text su titolo, autore, URL, categoria, trascrizione e riassunto.
- Ricerca vettoriale sugli embedding (pgvector, distanza coseno) per la ricerca semantica (`?mode=semantic`).
- Barra di stato durante l'elaborazione del video.
- Vista per locandine raggruppate per categoria, con eliminazione singola (cestino).
- Scheda video con player audio.
- Riassunto discorsivo breve e lungo.
- Tabella dei punti importanti con timestamp cliccabili.
- Export riassunto o trascrizione in TXT/PDF.
- Export trascrizione JSON con timestamp quando disponibili.
- Download audio MP3.

## Migrazione da SQLite

Se hai un vecchio archivio SQLite (`data/videos.db`), importalo in PostgreSQL
(idempotente sull'URL, genera gli embedding mancanti):

```bash
.venv\Scripts\python.exe scripts\migrate_sqlite_to_postgres.py
```
