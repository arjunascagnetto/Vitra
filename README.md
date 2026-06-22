# Video Transcript GUI

Web GUI locale per scaricare audio da link YouTube/VK Video, generare trascrizioni con timestamp, creare riassunti strutturati e archiviare ogni video in SQLite.

## Requisiti

- Python 3.11
- `OPENAI_API_KEY` configurata nell'ambiente per trascrizione audio e riassunto

## Avvio

```bash
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Configura la chiave in `.env`:

```bash
OPENAI_API_KEY="..."
OPENAI_SUMMARY_MODEL=gpt-5.5
```

Apri `http://127.0.0.1:8000`.

## Funzioni

- Import da YouTube o VK Video tramite URL pubblico.
- Categorie automatiche per separare i video.
- Ricerca su titolo, autore, URL, categoria, trascrizione e riassunto.
- Vista per locandine raggruppate per categoria.
- Scheda video con player audio.
- Riassunto discorsivo breve.
- Riassunto discorsivo lungo.
- Tabella dei punti importanti con timestamp cliccabili.
- Export riassunto o trascrizione in TXT/PDF.
- Export trascrizione JSON con timestamp quando disponibili.
- Download audio MP3.
