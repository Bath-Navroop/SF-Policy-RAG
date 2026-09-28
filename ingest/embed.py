"""Fill in chunks.embedding for every chunk that doesn't have one yet.

Run from the repo root (database running, GEMINI_API_KEY and EMBED_MODEL in .env):
    uv run python -m ingest.embed --limit 5    # try a tiny batch first
    uv run python -m ingest.embed              # then everything (~15 minutes)

Gemini's free tier for gemini-embedding-001 (checked 2026-09-26): 100 requests/min,
30,000 tokens/min, 1,000 requests/day. Measured on the first full run (2026-09-27):
EVERY TEXT COUNTS AS ONE REQUEST, even when 40 are sent in one API call. So a
full run (~860 chunks) uses most of a day's 1,000 requests.

How we stay inside the limits:
- Chunks are sent in batches of BATCH_SIZE (one API call per batch).
- A RateLimiter keeps what we've sent in ANY 60-second window under
  REQUESTS_PER_MINUTE and TOKENS_PER_MINUTE (a "sliding window"). Just averaging
  the rate isn't enough: the first run averaged under the limit but still had
  ~28K of 30K tokens inside one window.
- "Too many requests" (429) and temporary server errors are retried with
  growing waits (exponential backoff); each retry also goes through the limiter.

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

BATCH_SIZE = 30  # ~11K tokens; two batches fit in a minute under both limits below.
REQUESTS_PER_MINUTE = 70  # Free tier: 100. Each chunk counts as one request.
TOKENS_PER_MINUTE = 26_000  # Free tier: 30,000. First run: our estimate was ~2% below actual.
DAILY_REQUEST_LIMIT = 1_000
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


class RateLimiter:
    """Keeps usage in any 60-second window under a request limit and a token limit.

    It remembers when each batch was sent. Before sending a new one, it drops
    entries older than 60 seconds, adds up what's left, and if the new batch
    wouldn't fit, sleeps until the oldest entry falls out of the window.
    """

    WINDOW_SECONDS = 60

    def __init__(self, requests_per_minute: int, tokens_per_minute: int):
        self.requests_per_minute = requests_per_minute
        self.tokens_per_minute = tokens_per_minute
        self.sent: list[tuple[float, int, int]] = []  # (time sent, requests, tokens)

    def wait_for(self, requests: int, tokens: int) -> None:
        """Block until sending `requests` requests / `tokens` tokens stays within the limits."""
        while True:
            now = time.monotonic()
            self.sent = [entry for entry in self.sent if now - entry[0] < self.WINDOW_SECONDS]
            used_requests = sum(entry[1] for entry in self.sent)
            used_tokens = sum(entry[2] for entry in self.sent)
            fits = (
                used_requests + requests <= self.requests_per_minute
                and used_tokens + tokens <= self.tokens_per_minute
            )
            if fits or not self.sent:  # An empty window always lets one batch through.
                self.sent.append((now, requests, tokens))
                return
            oldest_time = self.sent[0][0]
            time.sleep(self.WINDOW_SECONDS - (now - oldest_time) + 0.5)


def embed_with_retry(texts: list[str], tokens: int, limiter: RateLimiter) -> list[list[float]]:
    """Call the API, retrying rate-limit and temporary errors with growing waits."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        limiter.wait_for(requests=len(texts), tokens=tokens)  # A retry uses quota too.
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
        if len(chunks) > DAILY_REQUEST_LIMIT * 0.8:
            print(
                f"Note: this uses ~{len(chunks)} of your {DAILY_REQUEST_LIMIT:,} requests/day. "
                "If it stops at the daily limit, run it again tomorrow."
            )

        limiter = RateLimiter(REQUESTS_PER_MINUTE, TOKENS_PER_MINUTE)
        done = 0
        for number, batch in enumerate(batches, start=1):
            texts = [embedding_text(chunk) for chunk in batch]
            tokens = sum(estimate_tokens(text) for text in texts)

            try:
                vectors = embed_with_retry(texts, tokens, limiter)
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

        embedded, total = conn.execute("SELECT count(embedding), count(*) FROM chunks").fetchone()
    print(f"Done. {embedded} of {total} chunks now have embeddings.")


if __name__ == "__main__":
    main()
