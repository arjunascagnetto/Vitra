"""One-shot migration of existing rows from the legacy SQLite database
(`data/videos.db`) into PostgreSQL, generating a pgvector embedding for each row.

Idempotent on `url`: a video already present in Postgres (same url) is skipped, so
the script can be re-run safely. Run after `init_db()` has created the schema.

    .venv\\Scripts\\python.exe scripts\\migrate_sqlite_to_postgres.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import DATA_DIR, get_connection, init_db
from app.main import build_embedding_text, embed_text, store_audio_path

SQLITE_PATH = DATA_DIR / "videos.db"

# Columns carried over verbatim from the old SQLite schema.
COPY_COLUMNS = [
    "url", "title", "uploader", "duration", "thumbnail", "webpage_url", "language",
    "category", "transcript", "transcript_json", "summary", "summary_short",
    "summary_long", "key_points_json", "audio_path", "audio_filename", "audio_mime",
    "status", "error", "created_at", "updated_at",
]


def main() -> None:
    if not SQLITE_PATH.exists():
        print(f"Nessun database SQLite in {SQLITE_PATH}, niente da migrare.")
        return

    init_db()

    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row
    rows = src.execute("SELECT * FROM videos ORDER BY id").fetchall()
    print(f"Trovate {len(rows)} righe in SQLite.")

    placeholders = ", ".join(["%s"] * (len(COPY_COLUMNS) + 1))  # +1 for embedding
    insert_sql = f"""
        INSERT INTO videos ({", ".join(COPY_COLUMNS)}, embedding)
        VALUES ({placeholders})
    """

    migrated = skipped = 0
    with get_connection() as db:
        for row in rows:
            data = {key: row[key] for key in row.keys()}
            exists = db.execute(
                "SELECT 1 FROM videos WHERE url = %s", (data["url"],)
            ).fetchone()
            if exists:
                skipped += 1
                print(f"SKIP (gia presente): {data['title']}")
                continue

            # The legacy SQLite audio_path may be absolute and from another machine.
            # Rewrite it to a project-relative path when the MP3 is present locally.
            audio_filename = data.get("audio_filename")
            if audio_filename and (DATA_DIR / "audio" / audio_filename).exists():
                data["audio_path"] = store_audio_path(DATA_DIR / "audio" / audio_filename)

            metadata = {"title": data.get("title")}
            embedding = embed_text(
                build_embedding_text(
                    metadata,
                    data.get("summary") or "",
                    data.get("transcript") or "",
                )
            )
            values = [data.get(col) for col in COPY_COLUMNS] + [embedding]
            db.execute(insert_sql, values)
            db.commit()
            migrated += 1
            print(f"MIGRATO: {data['title']}")

    src.close()
    print(f"Fatto. Migrate {migrated}, saltate {skipped}.")


if __name__ == "__main__":
    main()
