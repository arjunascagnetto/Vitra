from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import get_connection, init_db
from app.main import combined_summary_text, summarize


def main() -> None:
    init_db()
    with get_connection() as db:
        rows = db.execute(
            """
            SELECT id, title, uploader, duration, transcript_json, language
            FROM videos
            WHERE summary_short IS NULL
               OR summary_short = ''
               OR summary_long IS NULL
               OR summary_long = ''
               OR key_points_json IS NULL
               OR key_points_json = ''
            ORDER BY id
            """
        ).fetchall()
        for row in rows:
            print(f"START {row['id']}: {row['title']}", flush=True)
            segments = json.loads(row["transcript_json"])
            metadata = {
                "title": row["title"],
                "uploader": row["uploader"],
                "duration": row["duration"],
            }
            summary_data = summarize(segments, metadata, row["language"] or "auto")
            db.execute(
                """
                UPDATE videos
                SET summary = %s,
                    summary_short = %s,
                    summary_long = %s,
                    key_points_json = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (
                    combined_summary_text(summary_data),
                    summary_data.get("summary_short", ""),
                    summary_data.get("summary_long", ""),
                    json.dumps(summary_data.get("key_points", []), ensure_ascii=False),
                    row["id"],
                ),
            )
            db.commit()
            print(f"DONE {row['id']}", flush=True)


if __name__ == "__main__":
    main()
