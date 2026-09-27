"""Fill in chunks.embedding for every chunk that doesn't have one yet.

Run from the repo root (database running, GEMINI_API_KEY and EMBED_MODEL in .env):
    uv run python -m ingest.embed --limit 5    # try a tiny batch first
    uv run python -m ingest.embed              # then everything (~15 minutes)

Staying inside Gemini's free tier (checked 2026-09-26: 100 requests/min,
30,000 tokens/min, 1,000 requests/day):
- Chunks are sent in batches of BATCH_SIZE, one API request per batch.
- After each batch we wait long enough to average under TOKENS_PER_MINUTE.
- "Too many requests" (429) and temporary server errors are retried with
  growing waits (exponential backoff).

Safe to stop and re-run at any time (Ctrl+C, an error, or the daily limit):
every finished batch is saved immediately, and only chunks without an
embedding are picked up next time.
"""

import argparse
import sys
import time

import psycopg
from google.genai import errors

from app import config
from app.db import get_connection
from app.embeddings import embed_documents
from ingest.chunk import embedding_text

BATCH_SIZE = 40  # ~20K tokens per request, safely under the 30K-per-minute limit.
TOKENS_PER_MINUTE = 25_000  # A margin under the free tier's 30,000.
MAX_ATTEMPTS = 5
RETRYABLE_CODES = (429, 500, 503)  # Too many requests; server errors that usually pass.

SELECT_MISSING = """
    SELECT c.id, c.document_id, d.title, c.section_title, c.content
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE c.embedding IS NULL
    ORDER BY c.document_id, c.chunk_index
"""


def estimate_tokens(text: str) -> int:
    """Rough token count: about 4 characters per token for English text."""
    return len(text) // 4 + 1


def to_pgvector(values: list[float]) -> str:
    """Format a vector the way Postgres/pgvector reads it as text: '[0.1,0.2,...]'."""
    return "[" + ",".join(str(value) for value in values) + "]"


def fetch_missing(conn: psycopg.Connection, limit: int | None) -> list[dict]:
    """Chunks that still need an embedding, with what embedding_text() needs."""
    sql = SELECT_MISSING + (" LIMIT %s" if limit else "")
    rows = conn.execute(sql, (limit,) if limit else None).fetchall()
    return [
        {
            "id": row[0],
            "document_id": row[1],
            "doc_title": row[2],
            "section_title": row[3] or "",
            "content": row[4],
        }
        for row in rows
    ]


def embed_with_retry(texts: list[str]) -> list[list[float]]:
    """Call the API, retrying rate-limit and temporary errors with growing waits."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return embed_documents(texts)
        except errors.APIError as error:
            if error.code not in RETRYABLE_CODES or attempt == MAX_ATTEMPTS:
                raise
            wait = 30 * 2 ** (attempt - 1)  # 30s, 60s, 120s, 240s
            print(f"    Gemini said {error.code}; waiting {wait}s, then retrying...")
            time.sleep(wait)
    raise AssertionError("unreachable")  # The loop always returns or raises.


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed chunks that don't have an embedding yet.")
    parser.add_argument("--limit", type=int, help="only embed this many chunks (for a test run)")
    args = parser.parse_args()

    with get_connection() as conn:
        chunks = fetch_missing(conn, args.limit)
        batches = [chunks[i : i + BATCH_SIZE] for i in range(0, len(chunks), BATCH_SIZE)]
        print(
            f"Model {config.EMBED_MODEL}: {len(chunks)} chunks to embed in {len(batches)} batches."
        )

        done = 0
        for number, batch in enumerate(batches, start=1):
            texts = [embedding_text(chunk) for chunk in batch]
            tokens = sum(estimate_tokens(text) for text in texts)
            started = time.monotonic()

            try:
                vectors = embed_with_retry(texts)
            except errors.APIError as error:
                print(f"\nStopped: Gemini error {error.code}: {error.message}")
                print(
                    f"{done} chunks were saved. If this is the daily limit, run again "
                    "tomorrow; it will continue where it stopped."
                )
                sys.exit(1)

            with conn.transaction(), conn.cursor() as cur:
                cur.executemany(
                    "UPDATE chunks SET embedding = %s::vector WHERE id = %s",
                    [(to_pgvector(v), chunk["id"]) for v, chunk in zip(vectors, batch)],
                )
            done += len(batch)
            print(
                f"  batch {number}/{len(batches)}: {len(batch)} chunks, "
                f"~{tokens:,} tokens ({done}/{len(chunks)} done)"
            )

            # Pace ourselves: a batch of N tokens "uses up" N / TOKENS_PER_MINUTE minutes.
            if number < len(batches):
                pause = tokens / TOKENS_PER_MINUTE * 60 - (time.monotonic() - started)
                if pause > 0:
                    time.sleep(pause)

        embedded, total = conn.execute("SELECT count(embedding), count(*) FROM chunks").fetchone()
    print(f"Done. {embedded} of {total} chunks now have embeddings.")


if __name__ == "__main__":
    main()
