from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# PostgreSQL connection. Defaults to the local trust-auth instance; override with
# DATABASE_URL in .env. The pipeline stores transcripts, full-text search vectors
# (Italian config) and pgvector embeddings in a single `videos` table.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres@localhost:5432/transcript",
)

# Dimensionality of OpenAI text-embedding-3-small. Must match EMBEDDING_MODEL in
# app.main. Changing the model means changing this and re-embedding every row.
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1536"))

# Postgres text-search configuration used for the generated tsvector. Content is
# Italian; override with TS_CONFIG if a corpus is in another language.
TS_CONFIG = os.getenv("TS_CONFIG", "italian")


def get_connection() -> psycopg.Connection:
    connection = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    register_vector(connection)
    return connection


def init_db() -> None:
    with get_connection() as db:
        db.execute("CREATE EXTENSION IF NOT EXISTS vector")
        db.execute(
            f"""
            CREATE TABLE IF NOT EXISTS videos (
                id BIGSERIAL PRIMARY KEY,
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
                embedding vector({EMBEDDING_DIM}),
                status TEXT NOT NULL DEFAULT 'done',
                error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # Idempotent migrations for tables created by an older schema.
        ensure_column(db, "videos", "summary_short", "TEXT")
        ensure_column(db, "videos", "summary_long", "TEXT")
        ensure_column(db, "videos", "key_points_json", "TEXT")
        ensure_column(db, "videos", "audio_path", "TEXT")
        ensure_column(db, "videos", "audio_filename", "TEXT")
        ensure_column(db, "videos", "audio_mime", "TEXT")
        ensure_column(db, "videos", "embedding", f"vector({EMBEDDING_DIM})")
        ensure_column(db, "videos", "estimated_cost_usd", "DOUBLE PRECISION")
        # Per-segment Italian translation ([{start,end,text}]); null for Italian sources.
        ensure_column(db, "videos", "translation_json", "TEXT")

        # Full-text search replaces the old SQLite FTS5 virtual table + triggers:
        # a generated tsvector column kept in sync automatically, with a GIN index.
        db.execute(
            f"""
            ALTER TABLE videos
            ADD COLUMN IF NOT EXISTS search_vector tsvector
            GENERATED ALWAYS AS (
                to_tsvector(
                    '{TS_CONFIG}',
                    coalesce(title, '') || ' ' ||
                    coalesce(uploader, '') || ' ' ||
                    coalesce(url, '') || ' ' ||
                    coalesce(category, '') || ' ' ||
                    coalesce(transcript, '') || ' ' ||
                    coalesce(summary, '')
                )
            ) STORED
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS videos_search_idx ON videos USING GIN (search_vector)"
        )
        # IVFFlat needs rows present before it can be built well; cosine distance
        # matches how query embeddings are compared in app.main.search_semantic.
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS videos_embedding_idx
            ON videos USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)
            """
        )
        # Per-video chat history; cascades when a video is deleted.
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS video_messages (
                id BIGSERIAL PRIMARY KEY,
                video_id BIGINT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS video_messages_video_idx ON video_messages(video_id)"
        )
        db.commit()


def row_to_dict(row: Any) -> dict[str, Any]:
    # Rows already arrive as dicts via dict_row; copy to a plain dict so callers
    # can mutate freely (pop/replace keys) without touching the cursor row.
    return dict(row)


def ensure_column(db: psycopg.Connection, table: str, column: str, definition: str) -> None:
    db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
