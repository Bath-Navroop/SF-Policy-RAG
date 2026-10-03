"""Ask a question from the command line and see the whole pipeline.

    uv run python ask.py "When must officers turn on body cameras?"
    uv run python ask.py "..." -k 10      # retrieve more chunks
    uv run python ask.py "..." --full     # print each chunk's complete text

Prints the retrieved chunks (which source number each became, and whether the
answer cited it), the answer with its citations, and how long each step took.
Needs the database running and GEMINI_API_KEY in .env.

Each run costs 1 embedding request (of 1,000/day) + 1 Flash Lite request (of 500/day),
plus any retries.
"""

import argparse
import textwrap
import time

from app.db import get_connection
from app.embeddings import embed_query
from app.generate import DISCLAIMER, Answer, generate, source_label
from app.retrieval import TOP_K, search


def chunk_notes(chunks: list[dict], answer: Answer) -> list[str]:
    """For each chunk, which numbered source it became and whether the answer cited it.

    Two chunks from the same section share one source number (see build_sources).
    """
    source_of = {chunk_id: s["n"] for s in answer.sources for chunk_id in s["chunk_ids"]}
    cited = {s["n"] for s in answer.citations}
    notes = []
    for chunk in chunks:
        n = source_of[chunk["chunk_id"]]  # Every retrieved chunk is in some source.
        notes.append(f"source [{n}], {'cited' if n in cited else 'not cited'}")
    return notes


def print_chunks(chunks: list[dict], answer: Answer, full: bool) -> None:
    print(f"Retrieved {len(chunks)} chunks:")
    for rank, (chunk, note) in enumerate(zip(chunks, chunk_notes(chunks, answer)), start=1):
        path = f"§{chunk['section_path']}" if chunk["section_path"] else "(intro)"
        print(
            f"{rank:>2}. {chunk['document_id']} {path}  distance {chunk['distance']:.3f}  → {note}"
        )
        if full:
            print(textwrap.indent(chunk["content"], "      "))
        else:
            preview = " ".join(chunk["content"].split())  # Collapse line breaks for one line.
            print(f"      {preview[:160]}{'...' if len(preview) > 160 else ''}")
    print()


def print_answer(answer: Answer) -> None:
    print("Answer:")
    print(answer.display_text, "\n")  # The model's answer + the legal note (added by code).
    for source in answer.citations:
        print(f"[{source['n']}] {source_label(source)}\n    {source['url']}")
    if answer.invalid_citations:
        print(f"\nWarning: the answer cites sources that don't exist: {answer.invalid_citations}")
    if answer.refused:
        print("\n(The model said the sources don't answer this question.)")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask a question about SFPD policy.")
    parser.add_argument("question")
    parser.add_argument("-k", type=int, default=TOP_K, help=f"chunks to retrieve (default {TOP_K})")
    parser.add_argument("--full", action="store_true", help="print each chunk's full text")
    args = parser.parse_args()

    # Each step is called separately (not answer_question()) so each one can be timed.
    # Connecting to the database is left out of the timing: the API will keep its
    # connections open and reuse them, so it won't pay that cost per question.
    with get_connection() as conn:
        started = time.perf_counter()
        query_vector = embed_query(args.question)
        embedded = time.perf_counter()
        chunks = search(conn, query_vector, args.k)
        searched = time.perf_counter()
    answer = generate(args.question, chunks)
    finished = time.perf_counter()

    def ms(start: float, end: float) -> int:
        return round((end - start) * 1000)

    print(f"\nQuestion: {args.question}\n")
    print_chunks(chunks, answer, args.full)
    print_answer(answer)
    # "LLM" includes failed attempts and retry waits. "total" also covers building the
    # prompt and reading the answer (a few ms); Python's start-up time isn't counted.
    print(
        f"Timing: embed {ms(started, embedded)} ms · search {ms(embedded, searched)} ms · "
        f"LLM {answer.llm_latency_ms} ms · total {ms(started, finished)} ms"
    )
    if answer.model:  # Empty when no chunks were found and the LLM wasn't called.
        print(
            f"Tokens: {answer.input_tokens} in / {answer.output_tokens} out / "
            f"{answer.thinking_tokens} thinking · {answer.model}"
        )
    print(f"\n{DISCLAIMER}")


if __name__ == "__main__":
    main()
