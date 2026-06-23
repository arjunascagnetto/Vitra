"""Idempotent PostgreSQL setup: create the target database, enable pgvector and
create the schema. Driven by DATABASE_URL (default
postgresql://postgres@localhost:5432/transcript). Safe to re-run.

    python scripts/setup_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from app.database import DATABASE_URL, init_db


def main() -> None:
    parsed = urlparse(DATABASE_URL)
    dbname = parsed.path.lstrip("/") or "transcript"
    # Connect to the maintenance DB on the same server to create the target DB
    # (CREATE DATABASE cannot run inside the target connection or a transaction).
    admin_url = parsed._replace(path="/postgres").geturl()

    try:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
            ).fetchone()
            if exists:
                print(f"Database '{dbname}' gia presente.")
            else:
                conn.execute(f'CREATE DATABASE "{dbname}"')
                print(f"Creato database '{dbname}'.")
    except psycopg.OperationalError as exc:
        print(f"Impossibile connettersi a PostgreSQL ({admin_url}): {exc}", file=sys.stderr)
        print("Verifica che il servizio sia attivo e che DATABASE_URL sia corretto.", file=sys.stderr)
        sys.exit(1)

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        print("Estensione pgvector pronta.")

    init_db()
    print("Schema inizializzato. Database pronto.")


if __name__ == "__main__":
    main()
