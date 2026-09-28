"""Finding the chunks most relevant to a question (vector search, version 1).

Try it from the repo root (database running, GEMINI_API_KEY in .env):
    uv run python -m app.retrieval "When must officers turn on body cameras?"

How it works:
1. Embed the question with embed_query() -> 3,072 numbers.
2. Ask Postgres for the chunks whose embeddings are closest to it.
   `<=>` is pgvector's cosine distance: 0 = pointing the same way (very similar),
   1 = unrelated, 2 = opposite. With no index this checks all 857 chunks exactly,
   which takes milliseconds at this size (see PLAN.md Section 5).

Each question costs one embedding request (of the free tier's 1,000/day).
"""

import argparse

import psycopg

from app.db import get_connection, to_pgvector
from app.embeddings import embed_query

TOP_K = 5  # Starting value from PLAN.md; the Weeks 9-10 experiments try 3 / 5 / 10.

SEARCH_SQL = """
    SELECT c.id, c.document_id, d.title, c.section_path, c.section_title, c.content,
           d.effective_date, d.revised_date, d.source_url,
           c.embedding <=> %(query)s::vector AS distance
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE c.embedding IS NOT NULL
    ORDER BY distance
    LIMIT %(k)s
"""


def search(conn: psycopg.Connection, query_vector: list[float], k: int = TOP_K) -> list[dict]:
    """The k chunks closest to query_vector, closest first, with everything a citation needs."""
    rows = conn.execute(SEARCH_SQL, {"query": to_pgvector(query_vector), "k": k}).fetchall()
    return [
        {
            "chunk_id": row[0],
            "document_id": row[1],
            "doc_title": row[2],
            "section_path": row[3],
            "section_title": row[4],
            "content": row[5],
            "effective_date": row[6],
            "revised_date": row[7],
            "source_url": row[8],
            "distance": row[9],
        }
        for row in rows
    ]


def retrieve(conn: psycopg.Connection, question: str, k: int = TOP_K) -> list[dict]:
    """Embed the question, then return the k most similar chunks."""
    return search(conn, embed_query(question), k)


def main() -> None:
    parser = argparse.ArgumentParser(description="Show the chunks closest to a question.")
    parser.add_argument("question")
    parser.add_argument("-k", type=int, default=TOP_K, help=f"how many chunks (default {TOP_K})")
    args = parser.parse_args()

    with get_connection() as conn:
        results = retrieve(conn, args.question, args.k)

    for rank, chunk in enumerate(results, start=1):
        path = chunk["section_path"] or "(intro)"
        preview = " ".join(chunk["content"].split())[:160]
        print(f"{rank}. {chunk['document_id']} §{path}  distance {chunk['distance']:.3f}")
        print(f"   {chunk['doc_title']} — {chunk['section_title'] or ''}")
        print(f"   {preview}...\n")


if __name__ == "__main__":
    main()
