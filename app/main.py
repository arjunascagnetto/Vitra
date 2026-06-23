from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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
EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
OPENAI_AUDIO_LIMIT_BYTES = 24 * 1024 * 1024
# Keep embedding input well under the model's 8191-token limit (~4 chars/token).
EMBEDDING_INPUT_CHARS = 24000
AUDIO_DIR = DATA_DIR / "audio"

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


@app.get("/api/videos/{video_id}")
def get_video(video_id: int) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM videos WHERE id = %s", (video_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Video non trovato")
    video = row_to_dict(row)
    video["transcript_json"] = json.loads(video["transcript_json"])
    video["key_points"] = json.loads(video.get("key_points_json") or "[]")
    if not video.get("summary_short"):
        video["summary_short"] = video.get("summary", "")
    if not video.get("summary_long"):
        video["summary_long"] = video.get("summary", "")
    return video


@app.post("/api/videos")
def process_video(
    url: str = Form(...),
    language_hint: str = Form("auto"),
) -> dict[str, Any]:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Inserisci un URL http/https valido")
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY non configurata")

    with tempfile.TemporaryDirectory(prefix="video-transcript-") as tmp:
        tmpdir = Path(tmp)
        metadata = download_metadata(url)
        audio_path = download_audio(url, tmpdir)
        prepared_audio_path = prepare_export_audio(audio_path, tmpdir)
        transcript_segments = transcribe_audio_file(prepared_audio_path, tmpdir, language_hint)
        transcript_text = segments_to_text(transcript_segments)

        summary_data = summarize(transcript_segments, metadata, language_hint)
        summary = combined_summary_text(summary_data)
        category = categorize_video(transcript_text, summary, metadata)
        embedding = embed_text(build_embedding_text(metadata, summary, transcript_text))
        saved_audio_path = persist_audio(prepared_audio_path, metadata)
        saved = save_video(
            url,
            category,
            metadata,
            transcript_text,
            transcript_segments,
            summary_data,
            language_hint,
            saved_audio_path,
            embedding,
        )

    return {"video": saved}


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
    if not video.get("audio_path"):
        raise HTTPException(status_code=404, detail="Audio non salvato per questo video")
    audio_path = Path(video["audio_path"])
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="File audio non trovato su disco")
    filename = video.get("audio_filename") or f"{safe_filename(video['title'])}.mp3"
    return FileResponse(audio_path, media_type=video.get("audio_mime") or "audio/mpeg", filename=filename)


@app.get("/api/videos/{video_id}/export/{kind}.{fmt}")
def export_video(video_id: int, kind: str, fmt: str) -> Response:
    if kind not in {"summary", "transcript"}:
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


def embed_text(text: str) -> list[float]:
    client = OpenAI()
    cleaned = (text or "").strip()[:EMBEDDING_INPUT_CHARS] or " "
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


def transcribe_audio_file(audio_path: Path, tmpdir: Path, language_hint: str) -> list[dict[str, Any]]:
    chunks = prepare_audio_chunks(audio_path, tmpdir)
    transcript_segments: list[dict[str, Any]] = []
    for chunk_path, offset in chunks:
        transcript_segments.extend(transcribe_audio(chunk_path, language_hint, offset))
    return transcript_segments


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


def transcribe_audio(audio_path: Path, language_hint: str, offset: float = 0.0) -> list[dict[str, Any]]:
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
    segments = getattr(transcript, "segments", None) or []
    if not segments:
        text = getattr(transcript, "text", "")
        return [{"start": offset, "end": None, "text": text}]
    return [
        {
            "start": add_offset(segment.get("start") if isinstance(segment, dict) else segment.start, offset),
            "end": add_offset(segment.get("end") if isinstance(segment, dict) else segment.end, offset),
            "text": segment.get("text") if isinstance(segment, dict) else segment.text,
        }
        for segment in segments
    ]


def add_offset(value: Any, offset: float) -> float | None:
    if value is None:
        return None
    return float(value) + offset


def summarize(transcript_segments: list[dict[str, Any]], metadata: dict[str, Any], language_hint: str) -> dict[str, Any]:
    client = OpenAI()
    language_instruction = {
        "it": "Rispondi in italiano.",
        "ru": "Rispondi in russo.",
        "auto": "Rispondi nella lingua principale della trascrizione.",
    }.get(language_hint, "Rispondi nella lingua principale della trascrizione.")
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
    language_hint: str,
    audio_path: Path,
    embedding: list[float] | None = None,
) -> dict[str, Any]:
    audio_filename = audio_path.name
    summary = combined_summary_text(summary_data)
    with get_connection() as db:
        new_id = db.execute(
            """
            INSERT INTO videos (
                url, title, uploader, duration, thumbnail, webpage_url, language,
                category, transcript, transcript_json, summary, summary_short, summary_long,
                key_points_json, audio_path, audio_filename, audio_mime, embedding
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                url,
                metadata.get("title") or "Senza titolo",
                metadata.get("uploader") or metadata.get("channel"),
                metadata.get("duration"),
                metadata.get("thumbnail"),
                metadata.get("webpage_url") or url,
                None if language_hint == "auto" else language_hint,
                category,
                transcript_text,
                json.dumps(transcript_segments, ensure_ascii=False),
                summary,
                summary_data.get("summary_short", ""),
                summary_data.get("summary_long", ""),
                json.dumps(summary_data.get("key_points", []), ensure_ascii=False),
                str(audio_path),
                audio_filename,
                "audio/mpeg",
                embedding,
            ),
        ).fetchone()["id"]
        db.commit()
        row = db.execute("SELECT * FROM videos WHERE id = %s", (new_id,)).fetchone()
    saved = row_to_dict(row)
    saved.pop("transcript_json", None)
    saved.pop("embedding", None)
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
    body = video["summary"] if kind == "summary" else video["transcript"]
    label = "Riassunto" if kind == "summary" else "Trascrizione"
    return f"{video['title']}\nCategoria: {video['category']}\nURL: {video['url']}\n\n{label}\n\n{body}\n"


def write_pdf(path: Path, title: str, content: str) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.add_font("DejaVu", "", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    pdf.set_font("DejaVu", size=12)
    pdf.multi_cell(0, 8, content)
    pdf.output(path)


def safe_filename(value: str) -> str:
    allowed = [char if char.isalnum() or char in ("-", "_") else "-" for char in value.lower()]
    return "".join(allowed).strip("-")[:80] or "video"
