"""Tests for ingest/chunk.py. Run from the repo root: uv run pytest

These use small made-up sections and a small max_words, so each test is easy to
check by hand and doesn't depend on the downloaded data.
"""

from ingest.chunk import (
    chunk_document,
    chunk_section,
    embedding_text,
    split_into_units,
    split_words,
    word_count,
)


def words(n: int, word: str = "word") -> str:
    """Make a string of n words, e.g. words(3) -> 'word1 word2 word3'."""
    return " ".join(f"{word}{i}" for i in range(1, n + 1))


def make_section(text: str, number: str = "10.11.05") -> dict:
    return {"number": number, "heading": f"{number} ACTIVATION", "text": text}


def test_short_section_is_one_chunk():
    chunks = chunk_section(make_section(words(30)), max_words=50)
    assert len(chunks) == 1
    assert chunks[0]["section_path"] == "10.11.05"
    assert chunks[0]["content"] == words(30)


def test_empty_section_gives_no_chunks():
    assert chunk_section(make_section(""), max_words=50) == []


def test_nested_lines_stay_with_their_item():
    text = "A. first item\n  1. nested one\n  2. nested two\nB. second item"
    units = split_into_units(text)
    assert [u["label"] for u in units] == ["A", "B"]
    assert units[0]["text"] == "A. first item\n  1. nested one\n  2. nested two"


def test_long_section_splits_on_items_not_mid_item():
    # Four items of 20 words each; max 45 words fits two items per chunk.
    text = "\n".join(f"{label}. {words(19)}" for label in "ABCD")
    chunks = chunk_section(make_section(text), max_words=45)
    assert [c["section_path"] for c in chunks] == ["10.11.05.A-B", "10.11.05.C-D"]
    for chunk in chunks:
        assert chunk["content"].startswith(("A.", "C."))  # Each chunk begins at an item.


def test_no_chunk_is_over_the_limit():
    text = "\n".join(f"{label}. {words(30)}" for label in "ABCDE") + "\nF. " + words(200)
    chunks = chunk_section(make_section(text), max_words=60, overlap=10)
    assert all(word_count(c["content"]) <= 60 for c in chunks)


def test_word_windows_overlap_by_the_right_amount():
    windows = split_words(words(100), max_words=40, overlap=10)
    assert all(word_count(w) <= 40 for w in windows)
    for first, second in zip(windows, windows[1:]):
        assert first.split()[-10:] == second.split()[:10]  # Last 10 of one = first 10 of next.
    assert windows[-1].split()[-1] == "word100"  # Nothing lost at the end.


def test_word_windows_keep_line_breaks():
    text = "A. " + words(30) + "\n  1. " + words(30, "nested")
    windows = split_words(text, max_words=40, overlap=5)
    assert "\n  1. nested1" in windows[0] or "\n  1. nested1" in windows[1]


def test_chunk_document_adds_metadata_and_indexes():
    doc = {
        "doc_id": "DGO-10.11",
        "title": "Body Worn Cameras",
        "sections": [
            make_section(words(10), "10.11.01"),
            make_section("\n".join(f"{label}. {words(19)}" for label in "ABC"), "10.11.05"),
        ],
    }
    chunks = chunk_document(doc, max_words=45)
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert chunk["document_id"] == "DGO-10.11"
        assert chunk["doc_title"] == "Body Worn Cameras"
        assert chunk["section_title"].endswith("ACTIVATION")
        assert chunk["content"]


def test_embedding_text_has_context_prefix():
    chunk = {
        "document_id": "DGO-10.11",
        "doc_title": "Body Worn Cameras",
        "section_title": "10.11.05 ACTIVATION",
        "content": "A. Members must...",
    }
    assert embedding_text(chunk) == (
        "DGO 10.11 Body Worn Cameras — 10.11.05 ACTIVATION:\nA. Members must..."
    )
