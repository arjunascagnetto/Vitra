from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.requests import Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fpdf import FPDF
import imageio_ffmpeg
from openai import OpenAI
from dotenv import load_dotenv

from .database import BASE_DIR, DATA_DIR, TS_CONFIG, get_connection, init_db, row_to_dict


load_dotenv(BASE_DIR / ".env")

DEFAULT_SUMMARY_MODEL = "gpt-5.5"
SUMMARY_MODEL = os.getenv("OPENAI_SUMMARY_MODEL", DEFAULT_SUMMARY_MODEL)
EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

# Published OpenAI prices (USD), verified online; override via env if they change.
# Whisper is billed per audio minute; the summary model (gpt-5.4 by default) and
# the embedding model are billed per token.
WHISPER_USD_PER_MINUTE = float(os.getenv("WHISPER_USD_PER_MINUTE", "0.006"))
SUMMARY_USD_PER_1M_INPUT = float(os.getenv("SUMMARY_USD_PER_1M_INPUT", "2.50"))
SUMMARY_USD_PER_1M_OUTPUT = float(os.getenv("SUMMARY_USD_PER_1M_OUTPUT", "15.0"))
EMBEDDING_USD_PER_1M = float(os.getenv("EMBEDDING_USD_PER_1M", "0.02"))
# Heuristics to size the token-billed calls from the video duration before any
# transcript exists: ~150 spoken words/min, ~1.3 tokens/word -> ~200 tokens/min;
# the summary call emits a bounded JSON payload.
TRANSCRIPT_TOKENS_PER_MINUTE = float(os.getenv("TRANSCRIPT_TOKENS_PER_MINUTE", "200"))
SUMMARY_OUTPUT_TOKENS = int(os.getenv("SUMMARY_OUTPUT_TOKENS", "1500"))

# Local transcription (faster-whisper). Selectable per request; "openai" uses the
# Whisper API, "local" runs the model on this machine (free, no upload limit).
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "default")

OPENAI_AUDIO_LIMIT_BYTES = 24 * 1024 * 1024
# Sentinel for videos with no category; never stored in the `categories` table.
UNCATEGORIZED = "Senza categoria"
# Keep embedding input well under the model's 8192-token limit. EMBEDDING_INPUT_CHARS
# is a coarse first cut (~4 chars/token); embed_text then truncates precisely by
# tokens (tiktoken) to guarantee we stay under EMBEDDING_MAX_TOKENS.
EMBEDDING_INPUT_CHARS = 24000
EMBEDDING_MAX_TOKENS = 8000
AUDIO_DIR = DATA_DIR / "audio"
REPORTS_DIR = DATA_DIR / "reports"

app = FastAPI(title="Video Transcript GUI")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.exception_handler(Exception)
def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"detail": f"Errore interno: {type(exc).__name__}: {str(exc)}"},
    )


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/videos")
def list_videos(
    q: Annotated[str, Query(max_length=200)] = "",
    category: Annotated[str, Query(max_length=120)] = "",
    mode: Annotated[str, Query(max_length=20)] = "keyword",
) -> dict[str, Any]:
    columns = (
        "videos.id, videos.url, videos.title, videos.uploader, videos.duration, "
        "videos.thumbnail, videos.webpage_url, videos.language, videos.category, "
        "videos.summary, videos.created_at, videos.updated_at"
    )
    with get_connection() as db:
        params: list[Any] = []
        where: list[str] = []
        # Semantic search: order rows by cosine distance to the query embedding
        # (pgvector). Falls back to keyword search if the query is empty.
        if mode == "semantic" and q.strip():
            query_embedding = embed_text(q.strip())
            order_by = "videos.embedding <=> %s::vector"
            params.append(query_embedding)
            where.append("videos.embedding IS NOT NULL")
        else:
            order_by = "videos.created_at DESC"
            if q.strip():
                where.append(f"videos.search_vector @@ to_tsquery('{TS_CONFIG}', %s)")
                params.append(fts_query(q))
        if category.strip():
            where.append("videos.category = %s")
            params.append(category.strip())
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = db.execute(
            f"""
            SELECT {columns}
            FROM videos
            {clause}
            ORDER BY {order_by}
            """,
            params,
        ).fetchall()
        categories = db.execute(
            "SELECT category, COUNT(*) AS count FROM videos GROUP BY category ORDER BY category"
        ).fetchall()
    return {
        "videos": [row_to_dict(row) for row in rows],
        "categories": [row_to_dict(row) for row in categories],
    }


@app.get("/api/categories")
def list_all_categories() -> dict[str, Any]:
    # Canonical category list (table) with per-category video counts, including
    # categories that currently have no videos.
    with get_connection() as db:
        rows = db.execute(
            """
            SELECT c.name, COUNT(v.id) AS count
            FROM categories c
            LEFT JOIN videos v ON v.category = c.name
            GROUP BY c.name
            ORDER BY c.name
            """
        ).fetchall()
    return {"categories": [row_to_dict(row) for row in rows]}


@app.post("/api/categories")
def create_category(name: str = Form(...)) -> dict[str, Any]:
    name = name.strip()[:120]
    if not name or name == UNCATEGORIZED:
        raise HTTPException(status_code=400, detail="Nome categoria non valido")
    with get_connection() as db:
        db.execute(
            "INSERT INTO categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,)
        )
        db.commit()
    return {"name": name}


@app.delete("/api/categories/{name}")
def delete_category(name: str) -> dict[str, Any]:
    # Remove the category; its videos fall back to "uncategorized" (kept, not deleted).
    with get_connection() as db:
        db.execute("DELETE FROM categories WHERE name = %s", (name,))
        db.execute(
            "UPDATE videos SET category = %s, updated_at = now() WHERE category = %s",
            (UNCATEGORIZED, name),
        )
        db.commit()
    return {"deleted": name}


@app.put("/api/videos/{video_id}/category")
def set_video_category(video_id: int, category: str = Form("")) -> dict[str, Any]:
    category = category.strip()[:120] or UNCATEGORIZED
    with get_connection() as db:
        if not db.execute("SELECT 1 FROM videos WHERE id = %s", (video_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Video non trovato")
        # Creating-by-assignment: register the category unless it is the sentinel.
        if category != UNCATEGORIZED:
            db.execute(
                "INSERT INTO categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
                (category,),
            )
        db.execute(
            "UPDATE videos SET category = %s, updated_at = now() WHERE id = %s",
            (category, video_id),
        )
        db.commit()
    return {"id": video_id, "category": category}


@app.get("/api/videos/{video_id}")
def get_video(video_id: int) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM videos WHERE id = %s", (video_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Video non trovato")
    video = row_to_dict(row)
    # pgvector returns `embedding` as a numpy.ndarray, which is not JSON
    # serializable; it is internal and never needed by the frontend.
    video.pop("embedding", None)
    video["transcript_json"] = json.loads(video["transcript_json"])
    video["key_points"] = json.loads(video.get("key_points_json") or "[]")
    video["translation"] = json.loads(video.get("translation_json") or "[]")
    if not video.get("summary_short"):
        video["summary_short"] = video.get("summary", "")
    if not video.get("summary_long"):
        video["summary_long"] = video.get("summary", "")
    return video


CHAT_CONTEXT_CHARS = 60000

# Tavily web search for the chat agent. When the key is set, the model gets a
# `web_search` tool it can call when the answer isn't in the video.
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
TAVILY_MAX_RESULTS = int(os.getenv("TAVILY_MAX_RESULTS", "5"))
CHAT_MAX_TOOL_ITERATIONS = 3
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Cerca informazioni aggiornate sul web. Usalo solo quando la risposta non è "
            "presente nel contenuto del video o servono dati recenti. Restituisce risultati "
            "con titolo, url ed estratto."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "La query di ricerca."}
            },
            "required": ["query"],
        },
    },
}


GET_TRANSCRIPT_TOOL = {
    "type": "function",
    "function": {
        "name": "get_transcript",
        "description": (
            "Restituisce la trascrizione completa di un video dato il suo id (presente nel "
            "catalogo). Usalo quando i riassunti non bastano."
        ),
        "parameters": {
            "type": "object",
            "properties": {"video_id": {"type": "integer", "description": "L'id del video."}},
            "required": ["video_id"],
        },
    },
}
GET_VIDEO_CHAT_TOOL = {
    "type": "function",
    "function": {
        "name": "get_video_chat",
        "description": (
            "Restituisce la cronologia della chat per-video (domande e risposte già fatte su "
            "quel singolo video) dato il suo id. Utile per riusare conversazioni precedenti."
        ),
        "parameters": {
            "type": "object",
            "properties": {"video_id": {"type": "integer", "description": "L'id del video."}},
            "required": ["video_id"],
        },
    },
}
EXPORT_REPORT_TOOL = {
    "type": "function",
    "function": {
        "name": "export_report",
        "description": (
            "Esporta in un PDF scaricabile l'ultima risposta generata in questa "
            "conversazione (la usi quando l'utente chiede di esportare o salvare in PDF). "
            "Salva il report e restituisce l'URL di download: mostralo poi all'utente come "
            "link markdown [titolo](url)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Titolo breve del report (opzionale).",
                }
            },
        },
    },
}
GENERAL_CHAT_MAX_ITERATIONS = 6
GENERAL_TRANSCRIPT_CHARS = 40000
GENERAL_SUMMARY_LONG_CHARS = 1500


def tavily_search(query: str) -> list[dict[str, Any]]:
    if not TAVILY_API_KEY:
        return [{"error": "Ricerca web non configurata"}]
    payload = json.dumps(
        {
            "api_key": TAVILY_API_KEY,
            "query": query,
            "max_results": TAVILY_MAX_RESULTS,
            "search_depth": "basic",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.tavily.com/search",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read())
    except Exception as exc:  # network/HTTP errors must not break the chat
        return [{"error": f"Ricerca web fallita: {exc}"}]
    return [
        {"title": r.get("title"), "url": r.get("url"), "content": r.get("content")}
        for r in data.get("results", [])
    ]


def collect_web_sources(results: Any) -> list[dict[str, Any]]:
    # Keep only real hits (title + url) from a tavily_search result, for report
    # provenance. Error/empty payloads yield nothing.
    if not isinstance(results, list):
        return []
    return [
        {"title": r.get("title"), "url": r.get("url")}
        for r in results
        if isinstance(r, dict) and r.get("url")
    ]


@app.get("/api/videos/{video_id}/chat")
def list_chat_messages(video_id: int) -> dict[str, Any]:
    with get_connection() as db:
        if not db.execute("SELECT 1 FROM videos WHERE id = %s", (video_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Video non trovato")
        rows = db.execute(
            "SELECT role, content, created_at FROM video_messages WHERE video_id = %s ORDER BY id",
            (video_id,),
        ).fetchall()
    return {"messages": [row_to_dict(row) for row in rows]}


@app.post("/api/videos/{video_id}/chat")
def post_chat_message(video_id: int, message: str = Form(...)) -> dict[str, Any]:
    message = message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Messaggio vuoto")
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY non configurata")
    with get_connection() as db:
        video = db.execute(
            """
            SELECT id, title, summary_long, summary, transcript, translation_json
            FROM videos WHERE id = %s
            """,
            (video_id,),
        ).fetchone()
        if not video:
            raise HTTPException(status_code=404, detail="Video non trovato")
        history = db.execute(
            "SELECT role, content FROM video_messages WHERE video_id = %s ORDER BY id",
            (video_id,),
        ).fetchall()
        answer, refs = chat_with_video(
            row_to_dict(video), [row_to_dict(h) for h in history], message
        )
        db.execute(
            "INSERT INTO video_messages (video_id, role, content) VALUES (%s, 'user', %s)",
            (video_id, message),
        )
        db.execute(
            "INSERT INTO video_messages (video_id, role, content, refs_json) VALUES (%s, 'assistant', %s, %s)",
            (video_id, answer, json.dumps(refs, ensure_ascii=False)),
        )
        db.commit()
    return {"reply": {"role": "assistant", "content": answer}}


@app.delete("/api/videos/{video_id}/chat")
def reset_chat(video_id: int) -> dict[str, Any]:
    with get_connection() as db:
        db.execute("DELETE FROM video_messages WHERE video_id = %s", (video_id,))
        db.commit()
    return {"messages": []}


@app.post("/api/videos/{video_id}/chat/compact")
def compact_chat(video_id: int) -> dict[str, Any]:
    # Condense the conversation into a single recap message, freeing context while
    # keeping the gist for future turns.
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY non configurata")
    with get_connection() as db:
        rows = db.execute(
            "SELECT role, content FROM video_messages WHERE video_id = %s ORDER BY id",
            (video_id,),
        ).fetchall()
        if not rows:
            return {"messages": []}
        recap = summarize_conversation([row_to_dict(r) for r in rows])
        db.execute("DELETE FROM video_messages WHERE video_id = %s", (video_id,))
        db.execute(
            "INSERT INTO video_messages (video_id, role, content) VALUES (%s, 'assistant', %s)",
            (video_id, f"📝 Riepilogo conversazione precedente:\n{recap}"),
        )
        db.commit()
        new_rows = db.execute(
            "SELECT role, content, created_at FROM video_messages WHERE video_id = %s ORDER BY id",
            (video_id,),
        ).fetchall()
    return {"messages": [row_to_dict(r) for r in new_rows]}


def summarize_conversation(messages: list[dict[str, Any]]) -> str:
    client = OpenAI()
    convo = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in messages)
    try:
        response = client.chat.completions.create(
            model=SUMMARY_MODEL,
            messages=[
                {"role": "system", "content": "Riassumi conversazioni in italiano in modo fedele e conciso."},
                {
                    "role": "user",
                    "content": (
                        "Riassumi la seguente conversazione in un recap conciso ma completo, "
                        "mantenendo informazioni, domande e conclusioni utili per proseguire.\n\n"
                        + convo
                    ),
                },
            ],
            temperature=0.2,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Compattazione chat fallita: {str(exc)}") from exc
    return response.choices[0].message.content or ""


def chat_with_video(
    video: dict[str, Any], history: list[dict[str, Any]], message: str
) -> tuple[str, dict[str, Any]]:
    client = OpenAI()
    transcript = (video.get("transcript") or "")[:CHAT_CONTEXT_CHARS]
    summary_long = video.get("summary_long") or video.get("summary") or ""
    translation = ""
    if video.get("translation_json"):
        try:
            segments = json.loads(video["translation_json"])
            translation = "\n".join(str(s.get("text") or "") for s in segments)[:CHAT_CONTEXT_CHARS]
        except (TypeError, ValueError):
            translation = ""
    web_enabled = bool(TAVILY_API_KEY)
    system = (
        "Sei un assistente che risponde a domande su un singolo video, basandoti "
        "principalmente sul suo contenuto (trascrizione e riassunto). Rispondi sempre in "
        "italiano, in modo conciso e accurato.\n\n"
        f"Titolo: {video.get('title', '')}\n\n"
        f"Riassunto:\n{summary_long}\n\n"
        f"Trascrizione:\n{transcript}\n"
    )
    if translation:
        system += f"\nTraduzione italiana della trascrizione:\n{translation}\n"
    if web_enabled:
        system += (
            "\nHai a disposizione lo strumento `web_search`. Usalo SOLO quando l'informazione "
            "non è presente nel video o servono dati aggiornati. Quando usi il web, cita le "
            "fonti (URL). Dai sempre priorità al contenuto del video.\n"
        )
    else:
        system += "\nSe l'informazione non è presente nel video, dillo chiaramente.\n"
    system += (
        "\nQuando l'utente chiede di esportare o salvare in PDF l'ultima risposta, chiama lo "
        "strumento `export_report` e poi mostra all'utente il campo `download_url` restituito "
        "come link markdown, es. [Scarica il PDF](download_url).\n"
    )

    # Full history is kept and sent (the user manages size via Compatta/Reset).
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for item in history:
        if item.get("role") in ("user", "assistant"):
            messages.append({"role": item["role"], "content": item.get("content") or ""})
    messages.append({"role": "user", "content": message})

    # export_report is always available; web_search only when Tavily is configured.
    tools = ([WEB_SEARCH_TOOL] if web_enabled else []) + [EXPORT_REPORT_TOOL]

    video_id = video.get("id")
    web_sources: list[dict[str, Any]] = []

    def build_refs() -> dict[str, Any]:
        # The per-video chat is grounded in exactly this video.
        return {
            "video_ids": [video_id] if video_id else [],
            "web_sources": web_sources,
        }

    try:
        for _ in range(CHAT_MAX_TOOL_ITERATIONS):
            reply = client.chat.completions.create(
                model=SUMMARY_MODEL,
                messages=messages,
                temperature=0.3,
                tools=tools,
                tool_choice="auto",
            ).choices[0].message
            if not reply.tool_calls:
                return reply.content or "", build_refs()
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.function.name, "arguments": call.function.arguments},
                        }
                        for call in reply.tool_calls
                    ],
                }
            )
            for call in reply.tool_calls:
                args = json.loads(call.function.arguments or "{}")
                if call.function.name == "web_search":
                    result: Any = tavily_search(str(args.get("query", "")))
                    web_sources.extend(collect_web_sources(result))
                elif call.function.name == "export_report":
                    scope = f"video:{video_id}" if video_id else "general"
                    result = create_report(scope, video_id, args.get("title"))
                else:
                    result = [{"error": "Strumento non disponibile"}]
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        # Tool budget exhausted: force a final text answer without more tools.
        final = client.chat.completions.create(
            model=SUMMARY_MODEL, messages=messages, temperature=0.3
        )
        return final.choices[0].message.content or "", build_refs()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Chat LLM fallita: {str(exc)}") from exc


@app.get("/api/chat")
def list_general_messages() -> dict[str, Any]:
    with get_connection() as db:
        rows = db.execute(
            "SELECT role, content, created_at FROM general_messages ORDER BY id"
        ).fetchall()
    return {"messages": [row_to_dict(r) for r in rows]}


@app.post("/api/chat")
def post_general_message(
    message: str = Form(...),
    categories: list[str] = Form(default=[]),
) -> dict[str, Any]:
    message = message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Messaggio vuoto")
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY non configurata")
    selected = [c.strip() for c in categories if c.strip()]
    with get_connection() as db:
        history = db.execute(
            "SELECT role, content FROM general_messages ORDER BY id"
        ).fetchall()
        answer, refs = general_chat([row_to_dict(h) for h in history], message, selected)
        db.execute("INSERT INTO general_messages (role, content) VALUES ('user', %s)", (message,))
        db.execute(
            "INSERT INTO general_messages (role, content, refs_json) VALUES ('assistant', %s, %s)",
            (answer, json.dumps(refs, ensure_ascii=False)),
        )
        db.commit()
    return {"reply": {"role": "assistant", "content": answer}}


@app.delete("/api/chat")
def reset_general_chat() -> dict[str, Any]:
    with get_connection() as db:
        db.execute("DELETE FROM general_messages")
        db.commit()
    return {"messages": []}


@app.get("/api/reports")
def list_reports() -> dict[str, Any]:
    with get_connection() as db:
        rows = db.execute(
            """
            SELECT hash, title, scope, source_video_ids, web_sources, created_at
            FROM reports ORDER BY id DESC
            """
        ).fetchall()
    reports = []
    for row in rows:
        item = row_to_dict(row)
        item["source_video_ids"] = json.loads(item.get("source_video_ids") or "[]")
        item["web_sources"] = json.loads(item.get("web_sources") or "[]")
        item["download_url"] = f"/api/reports/{item['hash']}.pdf"
        reports.append(item)
    return {"reports": reports}


@app.get("/api/reports/{report_hash}.pdf")
def download_report(report_hash: str) -> FileResponse:
    # Content-addressed download: the filename is the sha256 in the reports table.
    if not re.fullmatch(r"[0-9a-f]{64}", report_hash):
        raise HTTPException(status_code=404, detail="Report non trovato")
    with get_connection() as db:
        row = db.execute(
            "SELECT title FROM reports WHERE hash = %s", (report_hash,)
        ).fetchone()
    pdf_path = REPORTS_DIR / f"{report_hash}.pdf"
    if not row or not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Report non trovato")
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"{safe_filename(row['title'])}.pdf",
    )


def general_chat(
    history: list[dict[str, Any]], message: str, categories: list[str]
) -> tuple[str, dict[str, Any]]:
    client = OpenAI()
    with get_connection() as db:
        if categories:
            rows = db.execute(
                """
                SELECT id, title, category, summary_short, summary_long, transcript
                FROM videos WHERE category = ANY(%s) ORDER BY id
                """,
                (categories,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, title, category, summary_short, summary_long, transcript FROM videos ORDER BY id"
            ).fetchall()
    videos = [row_to_dict(r) for r in rows]
    transcripts = {v["id"]: v.get("transcript") or "" for v in videos}

    catalog = "\n\n".join(
        f"[id={v['id']}] Categoria: {v.get('category') or 'Senza categoria'} | Titolo: {v.get('title')}\n"
        f"Riassunto breve: {(v.get('summary_short') or '').strip()}\n"
        f"Riassunto lungo: {(v.get('summary_long') or '')[:GENERAL_SUMMARY_LONG_CHARS].strip()}"
        for v in videos
    ) or "(nessun video nello scope selezionato)"

    web_enabled = bool(TAVILY_API_KEY)
    scope = (
        f"categorie selezionate: {', '.join(categories)}" if categories else "tutti i video"
    )
    system = (
        "Sei un assistente che risponde basandoti su un archivio di video (riassunti e "
        "trascrizioni). Rispondi sempre in italiano e cita i video pertinenti per titolo. "
        "Hai il catalogo qui sotto con id, categoria e riassunti breve/lungo. Per leggere la "
        "trascrizione completa di un video usa lo strumento get_transcript con il suo id; per "
        "rileggere le conversazioni già avute sulla chat di un singolo video usa get_video_chat.\n"
    )
    if web_enabled:
        system += (
            "Per informazioni non presenti nell'archivio puoi usare web_search; cita le fonti (URL).\n"
        )
    system += (
        "Quando l'utente chiede di esportare o salvare in PDF l'ultima risposta, chiama lo "
        "strumento export_report e poi mostra il campo download_url restituito come link "
        "markdown, es. [Scarica il PDF](download_url).\n"
    )
    system += f"\nScope: {scope}.\n\nCatalogo video:\n{catalog}\n"

    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for item in history:
        if item.get("role") in ("user", "assistant"):
            messages.append({"role": item["role"], "content": item.get("content") or ""})
    messages.append({"role": "user", "content": message})

    tools = [
        GET_TRANSCRIPT_TOOL,
        GET_VIDEO_CHAT_TOOL,
        EXPORT_REPORT_TOOL,
    ] + ([WEB_SEARCH_TOOL] if web_enabled else [])
    allowed_ids = set(transcripts)

    # Track which videos/web sources actually informed this turn, so a later
    # "esporta" can attribute the report (see create_report / refs_json).
    used_video_ids: set[int] = set()
    web_sources: list[dict[str, Any]] = []

    def build_refs(answer: str) -> dict[str, Any]:
        # Provenance = videos read via tools ∪ catalog videos cited by title in the
        # answer; fall back to the in-scope catalog so it's never empty for a real
        # answer (the model reasons over the catalog summaries even without tools).
        text = (answer or "").lower()
        cited = {
            v["id"]
            for v in videos
            if (v.get("title") or "").strip() and v["title"].lower() in text
        }
        ids = used_video_ids | cited
        if not ids:
            ids = set(allowed_ids)
        return {"video_ids": sorted(ids), "web_sources": web_sources}

    try:
        for _ in range(GENERAL_CHAT_MAX_ITERATIONS):
            reply = client.chat.completions.create(
                model=SUMMARY_MODEL, messages=messages, temperature=0.3, tools=tools, tool_choice="auto"
            ).choices[0].message
            if not reply.tool_calls:
                answer = reply.content or ""
                return answer, build_refs(answer)
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.function.name, "arguments": call.function.arguments},
                        }
                        for call in reply.tool_calls
                    ],
                }
            )
            for call in reply.tool_calls:
                args = json.loads(call.function.arguments or "{}")
                if call.function.name == "get_transcript":
                    vid = int(args.get("video_id", 0) or 0)
                    if vid in allowed_ids:
                        used_video_ids.add(vid)
                        result: Any = {"video_id": vid, "transcript": transcripts[vid][:GENERAL_TRANSCRIPT_CHARS]}
                    else:
                        result = {"error": "Video non disponibile nello scope selezionato"}
                elif call.function.name == "get_video_chat":
                    vid = int(args.get("video_id", 0) or 0)
                    if vid in allowed_ids:
                        used_video_ids.add(vid)
                        with get_connection() as cdb:
                            chat_rows = cdb.execute(
                                "SELECT role, content FROM video_messages WHERE video_id = %s ORDER BY id",
                                (vid,),
                            ).fetchall()
                        result = {"video_id": vid, "chat": [row_to_dict(m) for m in chat_rows]}
                    else:
                        result = {"error": "Video non disponibile nello scope selezionato"}
                elif call.function.name == "web_search":
                    result = tavily_search(str(args.get("query", "")))
                    web_sources.extend(collect_web_sources(result))
                elif call.function.name == "export_report":
                    result = create_report("general", None, args.get("title"))
                else:
                    result = {"error": "Strumento non disponibile"}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        final = client.chat.completions.create(
            model=SUMMARY_MODEL, messages=messages, temperature=0.3
        )
        answer = final.choices[0].message.content or ""
        return answer, build_refs(answer)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Chat generale fallita: {str(exc)}") from exc


def estimate_costs(
    duration_seconds: Any,
    transcription_backend: str = "openai",
    source_language: str | None = None,
) -> dict[str, float | None]:
    # Estimate the per-stage USD cost from the source duration, known from metadata
    # before any download/transcription. Returns None values when duration is
    # unknown (live streams etc.), since every stage scales with audio length.
    try:
        seconds = float(duration_seconds)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds <= 0:
        return {
            "transcription_usd": None,
            "summary_usd": None,
            "translation_usd": None,
            "embedding_usd": None,
            "total_usd": None,
        }

    minutes = seconds / 60.0
    transcript_tokens = minutes * TRANSCRIPT_TOKENS_PER_MINUTE

    # Local faster-whisper transcription is free; only the OpenAI API is billed.
    transcription = 0.0 if transcription_backend == "local" else minutes * WHISPER_USD_PER_MINUTE

    # `summarize`: (capped) timestamped transcript in, bounded JSON summary out.
    # `categorize_video`: short transcript + summary extract in, tiny category out.
    summary_input = min(transcript_tokens, 30000) + 400
    categorize_input = min(transcript_tokens, 3000) + min(SUMMARY_OUTPUT_TOKENS, 1500) + 200
    input_tokens = summary_input + categorize_input
    output_tokens = SUMMARY_OUTPUT_TOKENS + 20
    summary = (
        input_tokens * SUMMARY_USD_PER_1M_INPUT + output_tokens * SUMMARY_USD_PER_1M_OUTPUT
    ) / 1_000_000

    # Translation (non-Italian sources): the whole transcript goes in and a similar
    # volume of Italian text comes out.
    translation = 0.0
    if source_language is not None and not is_italian(source_language):
        translation = (
            transcript_tokens * SUMMARY_USD_PER_1M_INPUT
            + transcript_tokens * SUMMARY_USD_PER_1M_OUTPUT
        ) / 1_000_000

    embedding = min(transcript_tokens + 400, 6000) * EMBEDDING_USD_PER_1M / 1_000_000

    return {
        "transcription_usd": round(transcription, 4),
        "summary_usd": round(summary, 4),
        "translation_usd": round(translation, 4),
        "embedding_usd": round(embedding, 6),
        "total_usd": round(transcription + summary + translation + embedding, 4),
    }


@app.post("/api/videos/estimate")
def estimate_video(
    url: str = Form(...),
    transcription_backend: str = Form("openai"),
    language_hint: str = Form("auto"),
) -> dict[str, Any]:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Inserisci un URL http/https valido")
    metadata = download_metadata(url)
    duration = metadata.get("duration")
    transcription_model = WHISPER_MODEL if transcription_backend == "local" else "whisper-1"
    # Only an explicit non-Italian hint lets us pre-estimate translation; "auto"
    # language is unknown until after transcription.
    estimate_language = None if language_hint == "auto" else language_hint
    return {
        "title": metadata.get("title"),
        "duration": duration,
        "currency": "USD",
        "models": {
            "transcription": transcription_model,
            "summary": SUMMARY_MODEL,
            "embedding": EMBEDDING_MODEL,
        },
        "costs": estimate_costs(duration, transcription_backend, estimate_language),
    }


@app.post("/api/videos")
def process_video(
    url: str = Form(...),
    language_hint: str = Form("auto"),
    transcription_backend: str = Form("openai"),
) -> dict[str, Any]:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Inserisci un URL http/https valido")
    # Local transcription needs no API key, but summary/categorization/embedding
    # still call OpenAI for now, so the key remains required.
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY non configurata")

    with tempfile.TemporaryDirectory(prefix="video-transcript-") as tmp:
        tmpdir = Path(tmp)
        metadata = download_metadata(url)
        audio_path = download_audio(url, tmpdir)
        prepared_audio_path = prepare_export_audio(audio_path, tmpdir)
        transcript_segments, detected_language = transcribe_audio_file(
            prepared_audio_path, tmpdir, language_hint, transcription_backend
        )
        transcript_text = segments_to_text(transcript_segments)

        # Resolve the source language (explicit hint wins over detection) and, when
        # it is not Italian, produce a per-segment Italian translation.
        source_language = language_hint if language_hint != "auto" else detected_language
        translation_segments = None
        if not is_italian(source_language):
            translation_segments = translate_segments(transcript_segments, source_language)

        summary_data = summarize(transcript_segments, metadata, language_hint)
        summary = combined_summary_text(summary_data)
        category = categorize_video(transcript_text, summary, metadata)
        embedding = embed_text(build_embedding_text(metadata, summary, transcript_text))
        estimated_cost = estimate_costs(
            metadata.get("duration"), transcription_backend, source_language
        )["total_usd"]
        saved_audio_path = persist_audio(prepared_audio_path, metadata)
        saved = save_video(
            url,
            category,
            metadata,
            transcript_text,
            transcript_segments,
            summary_data,
            normalize_language(source_language),
            saved_audio_path,
            embedding,
            estimated_cost,
            translation_segments,
        )

    return {"video": saved}


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: int) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute(
            "SELECT audio_path FROM videos WHERE id = %s", (video_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Video non trovato")
        db.execute("DELETE FROM videos WHERE id = %s", (video_id,))
        db.commit()
    # Best-effort cleanup of the stored MP3; missing file is not an error.
    audio_path = row.get("audio_path")
    if audio_path:
        try:
            Path(audio_path).unlink(missing_ok=True)
        except OSError:
            pass
    return {"deleted": video_id}


def store_audio_path(path: Path) -> str:
    # Persist audio paths relative to the project root (POSIX separators) so the
    # DB stays portable across machines/OSes. Falls back to the absolute string
    # only if the file lives outside the project tree.
    try:
        return path.resolve().relative_to(BASE_DIR).as_posix()
    except ValueError:
        return str(path)


def resolve_audio_path(audio_path: str | None, audio_filename: str | None) -> Path | None:
    # Prefer the stored path (relative to project root, or absolute for legacy
    # rows), but fall back to data/audio/<filename> so rows whose audio_path
    # points at another machine still resolve as long as the MP3 was copied into
    # data/audio.
    if audio_path:
        candidate = Path(audio_path)
        if not candidate.is_absolute():
            candidate = BASE_DIR / candidate
        if candidate.exists():
            return candidate
    if audio_filename:
        local = AUDIO_DIR / audio_filename
        if local.exists():
            return local
    return None


@app.get("/api/videos/{video_id}/audio")
def download_video_audio(video_id: int) -> FileResponse:
    with get_connection() as db:
        row = db.execute(
            "SELECT title, audio_path, audio_filename, audio_mime FROM videos WHERE id = %s",
            (video_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Video non trovato")
    video = row_to_dict(row)
    audio_path = resolve_audio_path(video.get("audio_path"), video.get("audio_filename"))
    if audio_path is None:
        raise HTTPException(status_code=404, detail="File audio non trovato su disco")
    filename = video.get("audio_filename") or f"{safe_filename(video['title'])}.mp3"
    return FileResponse(audio_path, media_type=video.get("audio_mime") or "audio/mpeg", filename=filename)


@app.get("/api/videos/{video_id}/export/{kind}.{fmt}")
def export_video(video_id: int, kind: str, fmt: str) -> Response:
    if kind not in {"summary", "transcript", "translation"}:
        raise HTTPException(status_code=400, detail="Tipo export non valido")
    if fmt not in {"txt", "pdf", "json"}:
        raise HTTPException(status_code=400, detail="Formato export non valido")
    if fmt == "json" and kind != "transcript":
        raise HTTPException(status_code=400, detail="JSON disponibile solo per la trascrizione")

    with get_connection() as db:
        row = db.execute("SELECT * FROM videos WHERE id = %s", (video_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Video non trovato")
    video = row_to_dict(row)
    stem = safe_filename(video["title"] or f"video-{video_id}")

    if fmt == "json":
        return JSONResponse(
            json.loads(video["transcript_json"]),
            headers={"Content-Disposition": f'attachment; filename="{stem}-transcript.json"'},
        )

    content = build_export_text(video, kind)
    if fmt == "txt":
        return PlainTextResponse(
            content,
            headers={"Content-Disposition": f'attachment; filename="{stem}-{kind}.txt"'},
        )

    pdf_path = DATA_DIR / f"{stem}-{kind}.pdf"
    write_pdf(pdf_path, video["title"], content)
    return FileResponse(pdf_path, media_type="application/pdf", filename=pdf_path.name)


def fts_query(value: str) -> str:
    # Build a Postgres to_tsquery string with prefix matching, OR-joining terms
    # (mirrors the old SQLite `term*` behaviour). Sanitised so user input cannot
    # inject tsquery operators.
    terms = []
    for raw_term in value.strip().split():
        term = "".join(char for char in raw_term if char.isalnum() or char in ("_", "-"))
        if term:
            terms.append(f"{term}:*")
    return " | ".join(terms) or "''"


def build_embedding_text(metadata: dict[str, Any], summary: str, transcript_text: str) -> str:
    title = metadata.get("title") or ""
    combined = f"{title}\n{summary}\n{transcript_text}".strip()
    return combined[:EMBEDDING_INPUT_CHARS]


def truncate_to_tokens(text: str, max_tokens: int = EMBEDDING_MAX_TOKENS) -> str:
    # text-embedding-3-* have an 8192-token context limit; a char cap is not a
    # reliable proxy (token/char ratio varies by language). Truncate by tokens
    # with tiktoken when available, otherwise fall back to a conservative char cap.
    try:
        import tiktoken

        try:
            encoding = tiktoken.encoding_for_model(EMBEDDING_MODEL)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")
        tokens = encoding.encode(text)
        if len(tokens) <= max_tokens:
            return text
        return encoding.decode(tokens[:max_tokens])
    except Exception:
        # ~3 chars/token is conservative enough to stay under the limit.
        return text[: max_tokens * 3]


def embed_text(text: str) -> list[float]:
    client = OpenAI()
    cleaned = truncate_to_tokens((text or "").strip()) or " "
    try:
        response = client.embeddings.create(model=EMBEDDING_MODEL, input=cleaned)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Embedding OpenAI fallito: {str(exc)}") from exc
    return response.data[0].embedding


def run_yt_dlp(args: list[str]) -> subprocess.CompletedProcess[str]:
    # Invoke yt-dlp as a module with the current interpreter so it works on any
    # platform (the old hardcoded .venv/bin/yt-dlp path was Linux-only). Point it
    # at the bundled imageio-ffmpeg binary so post-processing uses our ffmpeg
    # rather than depending on a system install.
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ffmpeg-location",
            imageio_ffmpeg.get_ffmpeg_exe(),
            *args,
        ],
        check=True,
        text=True,
        capture_output=True,
    )


def download_metadata(url: str) -> dict[str, Any]:
    try:
        result = run_yt_dlp(["--dump-single-json", "--skip-download", url])
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=400, detail=f"Impossibile leggere il video: {exc.stderr.strip()}") from exc
    return json.loads(result.stdout)


def download_audio(url: str, tmpdir: Path) -> Path:
    output = tmpdir / "audio.%(ext)s"
    try:
        run_yt_dlp(["-f", "ba[acodec!=none]/b[acodec!=none]/18", "-o", str(output), url])
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=400, detail=f"Download audio fallito: {exc.stderr.strip()}") from exc
    candidates = list(tmpdir.glob("audio.*"))
    if not candidates:
        raise HTTPException(status_code=500, detail="Audio non generato")
    return candidates[0]


def prepare_export_audio(audio_path: Path, tmpdir: Path) -> Path:
    prepared = tmpdir / "audio-export.mp3"
    convert_audio(audio_path, prepared)
    return prepared


def transcribe_audio_file(
    audio_path: Path, tmpdir: Path, language_hint: str, backend: str = "openai"
) -> tuple[list[dict[str, Any]], str | None]:
    # Returns (segments, detected_language); the language lets the caller decide
    # whether a translation is needed when language_hint is "auto".
    if backend == "local":
        # faster-whisper handles arbitrarily long files, so no chunking/size limit.
        return transcribe_audio_local(audio_path, language_hint)
    chunks = prepare_audio_chunks(audio_path, tmpdir)
    transcript_segments: list[dict[str, Any]] = []
    detected_language: str | None = None
    for chunk_path, offset in chunks:
        segments, language = transcribe_audio(chunk_path, language_hint, offset)
        transcript_segments.extend(segments)
        if detected_language is None:
            detected_language = language
    return transcript_segments, detected_language


_local_whisper_model = None


def get_local_whisper_model():
    # Lazily load and cache the faster-whisper model (loading is expensive).
    global _local_whisper_model
    if _local_whisper_model is None:
        # Importing torch (CUDA build) first loads its bundled CUDA libraries
        # (cuBLAS/cuDNN) and registers their DLL directory, so CTranslate2 finds
        # them on the GPU without any separate NVIDIA runtime packages.
        try:
            import torch  # noqa: F401
        except ImportError:
            pass
        from faster_whisper import WhisperModel

        _local_whisper_model = WhisperModel(
            WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE
        )
    return _local_whisper_model


def transcribe_audio_local(
    audio_path: Path, language_hint: str
) -> tuple[list[dict[str, Any]], str | None]:
    model = get_local_whisper_model()
    language = None if language_hint == "auto" else language_hint
    try:
        segments, info = model.transcribe(str(audio_path), language=language, vad_filter=True)
        result = [
            {"start": float(seg.start), "end": float(seg.end), "text": seg.text}
            for seg in segments
        ]
        return result, getattr(info, "language", None)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Trascrizione locale fallita: {str(exc)}") from exc


def prepare_audio_chunks(audio_path: Path, tmpdir: Path) -> list[tuple[Path, float]]:
    compressed = tmpdir / "transcript-audio.mp3"
    if audio_path.suffix.lower() == ".mp3":
        compressed = audio_path
    else:
        convert_audio(audio_path, compressed)
    if compressed.stat().st_size <= OPENAI_AUDIO_LIMIT_BYTES:
        return [(compressed, 0.0)]

    for segment_seconds in (1200, 900, 600, 300):
        chunk_dir = tmpdir / f"chunks-{segment_seconds}"
        chunk_dir.mkdir(exist_ok=True)
        split_audio(compressed, chunk_dir, segment_seconds)
        chunks = sorted(chunk_dir.glob("chunk-*.mp3"))
        if chunks and all(chunk.stat().st_size <= OPENAI_AUDIO_LIMIT_BYTES for chunk in chunks):
            return [(chunk, index * float(segment_seconds)) for index, chunk in enumerate(chunks)]

    raise HTTPException(
        status_code=413,
        detail="Audio troppo grande anche dopo compressione e segmentazione. Serve un video più corto o un bitrate più basso.",
    )


def convert_audio(source: Path, destination: Path) -> None:
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "32k",
            str(destination),
        ],
        "Compressione audio fallita",
    )


def split_audio(source: Path, destination_dir: Path, segment_seconds: int) -> None:
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(source),
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            "-reset_timestamps",
            "1",
            "-c",
            "copy",
            str(destination_dir / "chunk-%03d.mp3"),
        ],
        "Divisione audio fallita",
    )


def run_ffmpeg(args: list[str], message: str) -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        subprocess.run([ffmpeg, *args], check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=500, detail=f"{message}: {exc.stderr.strip()}") from exc


def transcribe_audio(
    audio_path: Path, language_hint: str, offset: float = 0.0
) -> tuple[list[dict[str, Any]], str | None]:
    client = OpenAI()
    language = None if language_hint == "auto" else language_hint
    try:
        with audio_path.open("rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language=language,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Trascrizione OpenAI fallita: {str(exc)}") from exc
    detected_language = getattr(transcript, "language", None)
    segments = getattr(transcript, "segments", None) or []
    if not segments:
        text = getattr(transcript, "text", "")
        return [{"start": offset, "end": None, "text": text}], detected_language
    result = [
        {
            "start": add_offset(segment.get("start") if isinstance(segment, dict) else segment.start, offset),
            "end": add_offset(segment.get("end") if isinstance(segment, dict) else segment.end, offset),
            "text": segment.get("text") if isinstance(segment, dict) else segment.text,
        }
        for segment in segments
    ]
    return result, detected_language


def add_offset(value: Any, offset: float) -> float | None:
    if value is None:
        return None
    return float(value) + offset


_ITALIAN_NAMES = {"it", "ita", "italian", "italiano"}

# Map Whisper's language names to short codes for storage.
_LANGUAGE_CODES = {
    "italian": "it", "italiano": "it", "ita": "it",
    "russian": "ru", "russo": "ru", "rus": "ru",
    "english": "en", "inglese": "en", "eng": "en",
}


def is_italian(language: str | None) -> bool:
    return bool(language) and language.strip().lower() in _ITALIAN_NAMES


def normalize_language(language: str | None) -> str | None:
    if not language:
        return None
    value = language.strip().lower()
    return _LANGUAGE_CODES.get(value, value[:8])


# Translate the transcript in batches to keep each request within token limits.
TRANSLATION_BATCH_CHARS = 12000


def translate_segments(
    transcript_segments: list[dict[str, Any]], source_language: str | None
) -> list[dict[str, Any]]:
    client = OpenAI()
    translations: dict[int, str] = {}

    def flush(batch: list[tuple[int, str]]) -> None:
        if not batch:
            return
        items = [{"i": idx, "text": text} for idx, text in batch]
        prompt = (
            "Traduci in italiano il campo 'text' di ogni elemento del seguente array, "
            "mantenendo lo stesso indice 'i'. Non unire né dividere gli elementi, non "
            "aggiungere commenti. Restituisci solo JSON valido con chiave 'translations' "
            "= array di oggetti {\"i\": intero, \"text\": traduzione italiana}.\n\n"
            + json.dumps(items, ensure_ascii=False)
        )
        response = client.chat.completions.create(
            model=SUMMARY_MODEL,
            messages=[
                {"role": "system", "content": "Sei un traduttore professionale verso l'italiano."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        data = json.loads(response.choices[0].message.content or "{}")
        for item in data.get("translations", []):
            if isinstance(item, dict) and "i" in item:
                try:
                    translations[int(item["i"])] = str(item.get("text") or "").strip()
                except (TypeError, ValueError):
                    continue

    batch: list[tuple[int, str]] = []
    batch_chars = 0
    for idx, segment in enumerate(transcript_segments):
        text = str(segment.get("text") or "").strip()
        batch.append((idx, text))
        batch_chars += len(text)
        if batch_chars >= TRANSLATION_BATCH_CHARS:
            flush(batch)
            batch, batch_chars = [], 0
    flush(batch)

    # Align 1:1 with the originals; fall back to the original text if a segment
    # was dropped by the model.
    return [
        {
            "start": segment.get("start"),
            "end": segment.get("end"),
            "text": translations.get(idx) or str(segment.get("text") or "").strip(),
        }
        for idx, segment in enumerate(transcript_segments)
    ]


def summarize(transcript_segments: list[dict[str, Any]], metadata: dict[str, Any], language_hint: str) -> dict[str, Any]:
    client = OpenAI()
    # Summaries are always in Italian regardless of the source language; the
    # transcript stays in the original and a translation is produced separately.
    language_instruction = "Rispondi sempre in italiano, anche se la trascrizione è in un'altra lingua."
    title = metadata.get("title") or "Video"
    timestamped_transcript = timestamped_segments_for_prompt(transcript_segments)
    prompt = f"""
Titolo: {title}
Autore/canale: {metadata.get("uploader") or metadata.get("channel") or ""}
Durata in secondi: {metadata.get("duration") or ""}

Trascrizione con timestamp:
{timestamped_transcript[:120000]}

Devi analizzare il contenuto come un archivista/editor.
Produci tre elementi distinti:
1. "summary_short": un riassunto discorsivo breve, in 1-2 paragrafi, senza elenco puntato.
2. "summary_long": un riassunto discorsivo lungo, più completo, in 4-8 paragrafi, senza elenco puntato.
3. "key_points": una tabella logica dei passaggi più importanti. Ogni punto deve avere:
   - "time_seconds": secondo di inizio del passaggio, preso dai timestamp della trascrizione
   - "title": titolo breve del punto
   - "detail": spiegazione concreta del perché il punto è importante

Requisiti:
- Non inventare timestamp: usa solo tempi presenti o deducibili dai segmenti vicini.
- Dai priorità a tesi centrali, cambi di argomento, nomi propri, eventi, date, numeri, decisioni, conclusioni.
- Evita punti generici o duplicati.
- Se il video è lungo, crea 8-15 punti importanti.
- Restituisci solo JSON valido con chiavi: summary_short, summary_long, key_points.
{language_instruction}
"""
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_SUMMARY_MODEL", DEFAULT_SUMMARY_MODEL),
        messages=[
            {
                "role": "system",
                "content": "Sei un assistente specializzato nel trasformare trascrizioni video in riassunti editoriali e indici temporali consultabili.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"
    return normalize_summary_data(json.loads(content))


def normalize_summary_data(data: dict[str, Any]) -> dict[str, Any]:
    key_points = data.get("key_points") if isinstance(data.get("key_points"), list) else []
    normalized_points = []
    for point in key_points:
        if not isinstance(point, dict):
            continue
        try:
            time_seconds = max(0.0, float(point.get("time_seconds", 0)))
        except (TypeError, ValueError):
            time_seconds = 0.0
        normalized_points.append(
            {
                "time_seconds": time_seconds,
                "title": str(point.get("title") or "").strip(),
                "detail": str(point.get("detail") or "").strip(),
            }
        )
    return {
        "summary_short": str(data.get("summary_short") or "").strip(),
        "summary_long": str(data.get("summary_long") or "").strip(),
        "key_points": normalized_points,
    }


def combined_summary_text(summary_data: dict[str, Any]) -> str:
    lines = [
        "## Riassunto breve",
        summary_data.get("summary_short", ""),
        "",
        "## Riassunto lungo",
        summary_data.get("summary_long", ""),
        "",
        "## Punti importanti",
    ]
    for point in summary_data.get("key_points", []):
        lines.append(f"- [{format_timestamp(point.get('time_seconds'))}] {point.get('title')}: {point.get('detail')}")
    return "\n".join(lines).strip()


def timestamped_segments_for_prompt(segments: list[dict[str, Any]]) -> str:
    lines = []
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{format_timestamp(segment.get('start'))}] {text}")
    return "\n".join(lines)


def format_timestamp(seconds: Any) -> str:
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def categorize_video(transcript_text: str, summary: str, metadata: dict[str, Any]) -> str:
    fallback = fallback_category(metadata)
    client = OpenAI()
    prompt = f"""
Titolo: {metadata.get("title") or "Video"}
Autore: {metadata.get("uploader") or metadata.get("channel") or ""}
Categorie sorgente: {", ".join(metadata.get("categories") or [])}
Tags sorgente: {", ".join((metadata.get("tags") or [])[:12])}

Riassunto:
{summary[:6000]}

Estratto trascrizione:
{transcript_text[:12000]}

Assegna una singola categoria breve per archiviarlo.
Regole:
- massimo 3 parole
- niente punteggiatura
- usa italiano se possibile
- non usare categorie generiche come Video, Altro o Generale salvo contenuto davvero non classificabile
- restituisci solo la categoria, senza spiegazioni
"""
    try:
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_SUMMARY_MODEL", DEFAULT_SUMMARY_MODEL),
            messages=[
                {"role": "system", "content": "Classifichi video in categorie archivistiche concise."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=20,
        )
    except Exception:
        return fallback
    raw = response.choices[0].message.content or ""
    category = clean_category(raw)
    return category or fallback


def fallback_category(metadata: dict[str, Any]) -> str:
    categories = metadata.get("categories") or []
    if categories:
        return clean_category(str(categories[0])) or "Generale"
    tags = metadata.get("tags") or []
    if tags:
        return clean_category(str(tags[0])) or "Generale"
    return "Generale"


def clean_category(value: str) -> str:
    value = value.strip().strip("\"'`")
    cleaned = "".join(char if char.isalnum() or char.isspace() else " " for char in value)
    words = [word for word in cleaned.split() if word]
    blocked = {"video", "altro", "altri", "generico", "generale"}
    if not words:
        return ""
    category = " ".join(words[:3]).strip().title()
    if category.lower() in blocked:
        return ""
    return category[:80]


def save_video(
    url: str,
    category: str,
    metadata: dict[str, Any],
    transcript_text: str,
    transcript_segments: list[dict[str, Any]],
    summary_data: dict[str, Any],
    language: str | None,
    audio_path: Path,
    embedding: list[float] | None = None,
    estimated_cost_usd: float | None = None,
    translation_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    audio_filename = audio_path.name
    summary = combined_summary_text(summary_data)
    translation_json = (
        json.dumps(translation_segments, ensure_ascii=False) if translation_segments else None
    )
    with get_connection() as db:
        new_id = db.execute(
            """
            INSERT INTO videos (
                url, title, uploader, duration, thumbnail, webpage_url, language,
                category, transcript, transcript_json, summary, summary_short, summary_long,
                key_points_json, audio_path, audio_filename, audio_mime, embedding,
                estimated_cost_usd, translation_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                url,
                metadata.get("title") or "Senza titolo",
                metadata.get("uploader") or metadata.get("channel"),
                metadata.get("duration"),
                metadata.get("thumbnail"),
                metadata.get("webpage_url") or url,
                language,
                category,
                transcript_text,
                json.dumps(transcript_segments, ensure_ascii=False),
                summary,
                summary_data.get("summary_short", ""),
                summary_data.get("summary_long", ""),
                json.dumps(summary_data.get("key_points", []), ensure_ascii=False),
                store_audio_path(audio_path),
                audio_filename,
                "audio/mpeg",
                embedding,
                estimated_cost_usd,
                translation_json,
            ),
        ).fetchone()["id"]
        # Register the (AI-assigned) category in the canonical list.
        if category and category != UNCATEGORIZED:
            db.execute(
                "INSERT INTO categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
                (category,),
            )
        db.commit()
        row = db.execute("SELECT * FROM videos WHERE id = %s", (new_id,)).fetchone()
    saved = row_to_dict(row)
    saved.pop("transcript_json", None)
    saved.pop("embedding", None)
    saved.pop("translation_json", None)
    return saved


def persist_audio(audio_path: Path, metadata: dict[str, Any]) -> Path:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    video_id = metadata.get("id") or safe_filename(metadata.get("title") or "video")
    filename = f"{safe_filename(str(video_id))}-{safe_filename(metadata.get('title') or 'audio')}.mp3"
    destination = unique_path(AUDIO_DIR / filename)
    destination.write_bytes(audio_path.read_bytes())
    return destination


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise HTTPException(status_code=500, detail="Impossibile creare un nome file audio univoco")


def parse_vtt(content: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    lines = [line.strip() for line in content.splitlines()]
    i = 0
    while i < len(lines):
        if "-->" not in lines[i]:
            i += 1
            continue
        start_raw, end_raw = lines[i].split("-->", 1)
        start = parse_timestamp(start_raw.strip())
        end = parse_timestamp(end_raw.split()[0].strip())
        i += 1
        text_lines: list[str] = []
        while i < len(lines) and lines[i]:
            if not lines[i].startswith(("NOTE", "STYLE")):
                text_lines.append(clean_caption_text(lines[i]))
            i += 1
        text = " ".join(text_lines).strip()
        if text and (not segments or segments[-1]["text"] != text):
            segments.append({"start": start, "end": end, "text": text})
        i += 1
    return segments


def parse_timestamp(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    seconds = float(parts[-1])
    minutes = int(parts[-2]) if len(parts) >= 2 else 0
    hours = int(parts[-3]) if len(parts) >= 3 else 0
    return hours * 3600 + minutes * 60 + seconds


def clean_caption_text(value: str) -> str:
    cleaned = value.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    while "<" in cleaned and ">" in cleaned:
        start = cleaned.find("<")
        end = cleaned.find(">", start)
        if end == -1:
            break
        cleaned = cleaned[:start] + cleaned[end + 1 :]
    return " ".join(cleaned.split())


def segments_to_text(segments: list[dict[str, Any]]) -> str:
    return "\n".join(segment["text"].strip() for segment in segments if segment.get("text"))


def build_export_text(video: dict[str, Any], kind: str) -> str:
    if kind == "summary":
        body, label = video["summary"], "Riassunto"
    elif kind == "translation":
        segments = json.loads(video.get("translation_json") or "[]")
        body = "\n".join(
            f"[{format_timestamp(seg.get('start'))}] {seg.get('text', '')}" for seg in segments
        )
        label = "Traduzione (italiano)"
    else:
        body, label = video["transcript"], "Trascrizione"
    return f"{video['title']}\nCategoria: {video['category']}\nURL: {video['url']}\n\n{label}\n\n{body}\n"


# Unicode-capable TTF for PDF output. The bundled DejaVuSans (Latin + Cyrillic)
# is preferred and portable; the rest are last-resort fallbacks per platform.
PDF_FONT_CANDIDATES = [
    BASE_DIR / "static" / "fonts" / "DejaVuSans.ttf",
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path(r"C:\Windows\Fonts\DejaVuSans.ttf"),
    Path(r"C:\Windows\Fonts\arial.ttf"),
    Path(r"C:\Windows\Fonts\segoeui.ttf"),
]


def resolve_pdf_font() -> str:
    for candidate in PDF_FONT_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    raise HTTPException(
        status_code=500,
        detail="Nessun font TTF disponibile per il PDF (atteso static/fonts/DejaVuSans.ttf)",
    )


def render_pdf_bytes(content: str) -> bytes:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.add_font("PDFFont", "", resolve_pdf_font())
    pdf.set_font("PDFFont", size=12)
    pdf.multi_cell(0, 8, content)
    # fpdf2 returns a bytearray when no destination path is given.
    return bytes(pdf.output())


def write_pdf(path: Path, title: str, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_pdf_bytes(content))


def safe_filename(value: str) -> str:
    allowed = [char if char.isalnum() or char in ("-", "_") else "-" for char in value.lower()]
    return "".join(allowed).strip("-")[:80] or "video"


def first_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:80]
    return ""


def build_report_text(
    title: str,
    question: str,
    answer: str,
    video_refs: list[dict[str, Any]],
    web_sources: list[dict[str, Any]],
) -> str:
    lines = [title, f"Generato il {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    if (question or "").strip():
        lines += ["Domanda:", question.strip(), ""]
    lines += ["Risposta:", (answer or "").strip(), ""]
    if video_refs or web_sources:
        lines.append("Fonti:")
        for v in video_refs:
            lines.append(f"- Video [{v['id']}] {(v.get('title') or '').strip()}".rstrip())
        for s in web_sources:
            label = s.get("title") or s.get("url") or ""
            lines.append(f"- Web: {label} ({s.get('url') or ''})")
    return "\n".join(lines) + "\n"


def create_report(scope: str, video_id: int | None, title: str | None) -> dict[str, Any]:
    # Turn the last assistant answer of a chat into a stored, downloadable PDF.
    # Content-addressed: the file is named by the sha256 of its bytes, so exporting
    # the identical interaction twice reuses the same file. Records provenance
    # (videos consulted + web sources) taken from that message's refs_json.
    table = "video_messages" if video_id else "general_messages"
    with get_connection() as db:
        where = ["role = 'assistant'"]
        params: list[Any] = []
        if video_id:
            where.append("video_id = %s")
            params.append(video_id)
        last = db.execute(
            f"SELECT id, content, refs_json FROM {table} WHERE {' AND '.join(where)} "
            "ORDER BY id DESC LIMIT 1",
            tuple(params),
        ).fetchone()
        if not last:
            return {"error": "Nessuna risposta da esportare in questa conversazione."}

        answer = last["content"] or ""
        try:
            refs = json.loads(last.get("refs_json") or "{}")
        except (TypeError, ValueError):
            refs = {}
        ref_video_ids = [int(v) for v in (refs.get("video_ids") or [])]
        web_sources = refs.get("web_sources") or []

        uwhere = ["role = 'user'", "id < %s"]
        uparams: list[Any] = [last["id"]]
        if video_id:
            uwhere.append("video_id = %s")
            uparams.append(video_id)
        prev_user = db.execute(
            f"SELECT content FROM {table} WHERE {' AND '.join(uwhere)} ORDER BY id DESC LIMIT 1",
            tuple(uparams),
        ).fetchone()
        question = prev_user["content"] if prev_user else ""

        video_refs: list[dict[str, Any]] = []
        if ref_video_ids:
            rows = db.execute(
                "SELECT id, title FROM videos WHERE id = ANY(%s) ORDER BY id",
                (ref_video_ids,),
            ).fetchall()
            video_refs = [{"id": r["id"], "title": r["title"]} for r in rows]

        if video_id:
            chat_rows = db.execute(
                f"SELECT role, content FROM {table} WHERE video_id = %s ORDER BY id",
                (video_id,),
            ).fetchall()
        else:
            chat_rows = db.execute(
                f"SELECT role, content FROM {table} ORDER BY id"
            ).fetchall()
        chat_json = [row_to_dict(r) for r in chat_rows]

        report_title = (title or "").strip() or first_line(answer) or "Report chat"
        body = build_report_text(report_title, question, answer, video_refs, web_sources)
        pdf_bytes = render_pdf_bytes(body)
        digest = hashlib.sha256(pdf_bytes).hexdigest()
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        pdf_path = REPORTS_DIR / f"{digest}.pdf"
        if not pdf_path.exists():
            pdf_path.write_bytes(pdf_bytes)

        db.execute(
            """
            INSERT INTO reports
                (hash, title, scope, content, source_video_ids, web_sources, chat_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (hash) DO NOTHING
            """,
            (
                digest,
                report_title,
                scope,
                body,
                json.dumps(video_refs, ensure_ascii=False),
                json.dumps(web_sources, ensure_ascii=False),
                json.dumps(chat_json, ensure_ascii=False),
            ),
        )
        db.commit()

    return {
        "status": "ok",
        "title": report_title,
        "download_url": f"/api/reports/{digest}.pdf",
    }
