from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "videos.db"


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with get_connection() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                uploader TEXT,
                duration INTEGER,
                thumbnail TEXT,
                webpage_url TEXT,
                language TEXT,
                category TEXT NOT NULL DEFAULT 'Senza categoria',
                transcript TEXT NOT NULL,
                transcript_json TEXT NOT NULL,
                summary TEXT NOT NULL,
                summary_short TEXT,
                summary_long TEXT,
                key_points_json TEXT,
                audio_path TEXT,
                audio_filename TEXT,
                audio_mime TEXT,
                status TEXT NOT NULL DEFAULT 'done',
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        ensure_column(db, "videos", "audio_path", "TEXT")
        ensure_column(db, "videos", "audio_filename", "TEXT")
        ensure_column(db, "videos", "audio_mime", "TEXT")
        ensure_column(db, "videos", "summary_short", "TEXT")
        ensure_column(db, "videos", "summary_long", "TEXT")
        ensure_column(db, "videos", "key_points_json", "TEXT")
        db.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS video_search
            USING fts5(title, uploader, url, category, transcript, summary, content='videos', content_rowid='id')
            """
        )
        db.execute(
            """
            CREATE TRIGGER IF NOT EXISTS videos_ai AFTER INSERT ON videos BEGIN
                INSERT INTO video_search(rowid, title, uploader, url, category, transcript, summary)
                VALUES (new.id, new.title, new.uploader, new.url, new.category, new.transcript, new.summary);
            END
            """
        )
        db.execute(
            """
            CREATE TRIGGER IF NOT EXISTS videos_au AFTER UPDATE ON videos BEGIN
                INSERT INTO video_search(video_search, rowid, title, uploader, url, category, transcript, summary)
                VALUES('delete', old.id, old.title, old.uploader, old.url, old.category, old.transcript, old.summary);
                INSERT INTO video_search(rowid, title, uploader, url, category, transcript, summary)
                VALUES (new.id, new.title, new.uploader, new.url, new.category, new.transcript, new.summary);
            END
            """
        )
        db.execute(
            """
            CREATE TRIGGER IF NOT EXISTS videos_ad AFTER DELETE ON videos BEGIN
                INSERT INTO video_search(video_search, rowid, title, uploader, url, category, transcript, summary)
                VALUES('delete', old.id, old.title, old.uploader, old.url, old.category, old.transcript, old.summary);
            END
            """
        )
        db.commit()


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
