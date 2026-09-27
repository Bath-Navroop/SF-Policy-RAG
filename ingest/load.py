"""Load parsed DGOs into Postgres: one `documents` row per order, one `chunks` row per chunk.

No embeddings yet; embed.py fills in chunks.embedding afterwards.

Run from the repo root (with the database running: `docker compose up -d`):
    uv run python -m ingest.load          # the 10 starter orders
    uv run python -m ingest.load --all    # every parsed order

Safe to run again: each order is updated in its own transaction, and an order whose
chunks haven't changed is left alone, so its embeddings (once we have them) are kept.
"""

import argparse
import csv

import psycopg

from app.db import get_connection
from ingest.chunk import chunk_document, load_parsed
from ingest.download import MANIFEST_PATH, STARTER_IDS

UPSERT_DOCUMENT = """
    INSERT INTO documents (id, title, source_type, source_url, effective_date, revised_date,
                           downloaded_at)
    VALUES (%(doc_id)s, %(title)s, 'dgo', %(source_url)s, %(effective_date)s, %(revised_date)s,
            %(downloaded_at)s)
    ON CONFLICT (id) DO UPDATE SET
        title = EXCLUDED.title,
        source_url = EXCLUDED.source_url,
        effective_date = EXCLUDED.effective_date,
        revised_date = EXCLUDED.revised_date,
        downloaded_at = EXCLUDED.downloaded_at
"""

INSERT_CHUNK = """
    INSERT INTO chunks (document_id, section_path, section_title, chunk_index, content)
    VALUES (%(document_id)s, %(section_path)s, %(section_title)s, %(chunk_index)s, %(content)s)
"""


def load_manifest() -> dict[str, dict]:
    with MANIFEST_PATH.open(newline="", encoding="utf-8") as file:
        return {row["doc_id"]: row for row in csv.DictReader(file)}


def chunk_rows(chunks: list[dict]) -> list[tuple]:
    """The parts of each chunk we compare to decide whether an order changed."""
    return [(c["section_path"], c["section_title"], c["content"]) for c in chunks]


def load_document(conn: psycopg.Connection, meta: dict) -> str:
    """Insert or update one order and its chunks. Returns what happened, for printing."""
    doc_id = meta["doc_id"]
    chunks = chunk_document(load_parsed(doc_id))
    for chunk in chunks:
        chunk["section_path"] = chunk["section_path"] or None  # Store "no path" as NULL.
        chunk["section_title"] = chunk["section_title"] or None

    # A transaction: either every statement below takes effect, or (on any error) none do,
    # so an order is never left half-loaded.
    with conn.transaction():
        conn.execute(
            UPSERT_DOCUMENT,
            {
                **meta,
                # Empty strings in the CSV mean "no date"; the database wants NULL.
                "effective_date": meta["effective_date"] or None,
                "revised_date": meta["revised_date"] or None,
            },
        )

        existing = conn.execute(
            "SELECT section_path, section_title, content FROM chunks "
            "WHERE document_id = %s ORDER BY chunk_index",
            (doc_id,),
        ).fetchall()
        if existing == chunk_rows(chunks):
            return f"unchanged, kept {len(chunks)} chunks"

        # Changed (or new): replace this order's chunks.
        conn.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))
        with conn.cursor() as cur:
            cur.executemany(INSERT_CHUNK, chunks)  # One statement, run once per chunk.
    action = "replaced" if existing else "loaded"
    return f"{action} {len(chunks)} chunks"


def main() -> None:
    parser = argparse.ArgumentParser(description="Load parsed DGOs and chunks into Postgres.")
    parser.add_argument("--all", action="store_true", help="load every order in the manifest")
    args = parser.parse_args()

    manifest = load_manifest()
    doc_ids = list(manifest) if args.all else STARTER_IDS

    with get_connection() as conn:
        for doc_id in doc_ids:
            print(f"  {doc_id:<10} {load_document(conn, manifest[doc_id])}")

        documents = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
        chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    print(f"Database now has {documents} documents and {chunks} chunks.")


if __name__ == "__main__":
    main()
