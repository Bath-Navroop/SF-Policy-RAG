"""Split parsed DGO sections (data/parsed/*.json) into chunks for search.

Run from the repo root:
    uv run python -m ingest.chunk                      # stats for the 10 starter orders
    uv run python -m ingest.chunk --all                # stats for every parsed order
    uv run python -m ingest.chunk DGO-10.11 --print    # show one order's chunks, to read by eye

This file doesn't write anything. The next step (loading into Postgres) imports
`chunk_document()` from here and stores what it returns.

How a section is split, from simplest to most fallback:
1. A section of up to MAX_WORDS words is one chunk. Most sections are (median ~175 words).
2. A longer section is split into "units": each top-level item (A., B., C. ...) together
   with everything nested under it, or a plain paragraph / table row. Neighbouring
   units are grouped into chunks of up to MAX_WORDS, never cutting a unit in half.
3. A single unit longer than MAX_WORDS is cut into overlapping word windows
   (OVERLAP_WORDS shared between neighbours), so a sentence cut at one boundary
   appears whole in the other chunk.

Each chunk records a section path for citations: "10.11.05" for a whole section,
"10.11.05.B" for one item, "10.11.05.B-D" for a group of items.
"""

import argparse
import json
import re
import statistics
from pathlib import Path

from ingest.download import STARTER_IDS
from ingest.parse import PARSED_DIR

MAX_WORDS = 400  # Starting value from PLAN.md; a Weeks 9-10 experiment compares 200/400/800.
OVERLAP_WORDS = 50

# A top-level item line: no indent, then a label like "A." "1." or "b." and a space.
# (Nested items are indented, so they don't match and stay with their parent.)
TOP_LEVEL_ITEM_RE = re.compile(r"^([A-Z]|[a-z]|\d{1,2})\. ")


def word_count(text: str) -> int:
    return len(text.split())


# ---------- Step 2: units ----------


def split_into_units(text: str) -> list[dict]:
    """Group a section's lines into units: a top-level item plus its nested lines,
    or a single plain line (paragraph or table row).

    Returns [{"label": "B" or None, "text": "..."}] in order.
    """
    units: list[dict] = []
    for line in text.split("\n"):
        match = TOP_LEVEL_ITEM_RE.match(line)
        if match:
            units.append({"label": match.group(1), "lines": [line]})
        elif line.startswith(" ") and units:
            units[-1]["lines"].append(line)  # Indented (nested) line: belongs to the item above.
        else:
            units.append({"label": None, "lines": [line]})
    return [{"label": u["label"], "text": "\n".join(u["lines"])} for u in units]


def group_units(units: list[dict], max_words: int) -> list[list[dict]]:
    """Greedily put neighbouring units together, starting a new group when the next
    unit would push the group over max_words. A unit that is too big on its own
    ends up alone in its group (step 3 splits it further).
    """
    groups: list[list[dict]] = []
    current: list[dict] = []
    current_words = 0
    for unit in units:
        words = word_count(unit["text"])
        if current and current_words + words > max_words:
            groups.append(current)
            current, current_words = [], 0
        current.append(unit)
        current_words += words
    if current:
        groups.append(current)
    return groups


def group_path(section_number: str, group: list[dict]) -> str:
    """Section path for a group of units: '10.11.05', '10.11.05.B' or '10.11.05.B-D'."""
    labels = [unit["label"] for unit in group if unit["label"]]
    if not labels:
        return section_number
    suffix = labels[0] if len(labels) == 1 else f"{labels[0]}-{labels[-1]}"
    return f"{section_number}.{suffix}" if section_number else suffix


# ---------- Step 3: overlapping word windows ----------


def split_words(text: str, max_words: int, overlap: int) -> list[str]:
    """Cut text into windows of at most max_words words; neighbours share `overlap` words.

    Each token is a word *plus the whitespace after it*, so joining tokens back
    together keeps the original line breaks and indentation.
    """
    tokens = re.findall(r"\S+\s*", text)
    if len(tokens) <= max_words:
        return [text]
    step = max_words - overlap  # How far each window moves forward.
    windows = []
    for start in range(0, len(tokens), step):
        windows.append("".join(tokens[start : start + max_words]).strip())
        if start + max_words >= len(tokens):
            break  # This window reached the end; don't add a tiny leftover window.
    return windows


# ---------- Putting it together ----------


def chunk_section(
    section: dict, max_words: int = MAX_WORDS, overlap: int = OVERLAP_WORDS
) -> list[dict]:
    """Split one parsed section into chunks: [{"section_path": ..., "content": ...}]."""
    text = section["text"].strip()
    number = section["number"]
    if not text:
        return []  # A heading with nothing under it.

    if word_count(text) <= max_words:  # Step 1: the whole section fits.
        return [{"section_path": number, "content": text}]

    chunks = []
    for group in group_units(split_into_units(text), max_words):  # Step 2.
        path = group_path(number, group)
        group_text = "\n".join(unit["text"] for unit in group)
        for piece in split_words(group_text, max_words, overlap):  # Step 3 (usually 1 piece).
            chunks.append({"section_path": path, "content": piece})
    return chunks


def chunk_document(
    doc: dict, max_words: int = MAX_WORDS, overlap: int = OVERLAP_WORDS
) -> list[dict]:
    """Split a parsed order into chunks with all the metadata the database needs."""
    chunks = []
    for section in doc["sections"]:
        for piece in chunk_section(section, max_words, overlap):
            chunks.append(
                {
                    "document_id": doc["doc_id"],
                    "doc_title": doc["title"],
                    "section_path": piece["section_path"],
                    "section_title": section["heading"],
                    "chunk_index": len(chunks),  # 0, 1, 2 ... in reading order.
                    "content": piece["content"],
                }
            )
    return chunks


def embedding_text(chunk: dict) -> str:
    """The text we'll send to the embedding model: a context line, then the content.

    e.g. "DGO 10.11 Body Worn Cameras — 10.11.05 ACTIVATION OF BODY WORN CAMERAS: ..."
    Kept separate from `content` (not stored inside it) so the Weeks 9-10 experiment
    "with vs without context prefix" only needs re-embedding, not re-chunking.
    """
    doc_label = f"{chunk['document_id'].replace('-', ' ')} {chunk['doc_title']}"
    if chunk["section_title"]:
        doc_label += f" — {chunk['section_title']}"
    return f"{doc_label}:\n{chunk['content']}"


# ---------- Running it ----------


def load_parsed(doc_id: str) -> dict:
    return json.loads((PARSED_DIR / f"{doc_id}.json").read_text(encoding="utf-8"))


def print_chunks(chunks: list[dict]) -> None:
    """Print chunks as Markdown for checking by eye (blank lines keep line breaks in a preview)."""
    for chunk in chunks:
        path = chunk["section_path"] or "(intro)"
        print(f"## Chunk {chunk['chunk_index']} — {path} ({word_count(chunk['content'])} words)\n")
        print("\n\n".join(embedding_text(chunk).split("\n")) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Split parsed DGOs into chunks.")
    parser.add_argument(
        "doc_ids", nargs="*", help="orders to chunk (default: the 10 starter orders)"
    )
    parser.add_argument("--all", action="store_true", help="chunk every parsed order")
    parser.add_argument("--print", action="store_true", help="print the chunks to read by eye")
    args = parser.parse_args()

    if args.all:
        doc_ids = sorted(path.stem for path in Path(PARSED_DIR).glob("DGO-*.json"))
    else:
        doc_ids = args.doc_ids or STARTER_IDS

    all_sizes = []
    for doc_id in doc_ids:
        chunks = chunk_document(load_parsed(doc_id))
        sizes = [word_count(chunk["content"]) for chunk in chunks]
        all_sizes.extend(sizes)
        print(f"  {doc_id:<10} {len(chunks):3} chunks, largest {max(sizes, default=0):4} words")
        if args.print:
            print()
            print_chunks(chunks)

    if all_sizes:
        print(
            f"Total: {len(all_sizes)} chunks from {len(doc_ids)} orders; words per chunk "
            f"min {min(all_sizes)}, median {statistics.median(all_sizes):.0f}, max {max(all_sizes)}"
        )


if __name__ == "__main__":
    main()
