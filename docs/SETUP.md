# Setup

## Environment

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env`:

```bash
OPENAI_API_KEY=...
OPENAI_SUMMARY_MODEL=gpt-5.5
```

## Run

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

## Verify

```bash
.venv/bin/python -m compileall app
node --check static/app.js
```

## Backfill Existing Rows

```bash
.venv/bin/python scripts/backfill_summaries.py
```
