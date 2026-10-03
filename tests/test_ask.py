"""Tests for ask.py. No API calls and no database: those pieces are replaced with fakes."""

import sys
from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace

import ask
from app import generate


def make_chunk(chunk_id, section_path, chunk_index=0):
    return {
        "chunk_id": chunk_id,
        "document_id": "DGO-10.11",
        "doc_title": "Body Worn Cameras",
        "section_path": section_path,
        "section_title": "10.11.05 ACTIVATION",
        "chunk_index": chunk_index,
        "content": "Members shall activate the BWC.",
        "effective_date": date(2025, 11, 6),
        "revised_date": None,
        "source_url": "https://example.org/10-11",
        "distance": 0.2,
    }


def test_chunk_notes_show_source_number_and_whether_cited():
    chunks = [
        make_chunk(1, "10.11.05.A-C"),
        make_chunk(2, "10.11.02"),
        make_chunk(3, "10.11.05.A-C", 1),
    ]
    sources = generate.build_sources(chunks)  # Chunks 1 and 3 share a section -> one source.
    answer = generate.Answer(
        question="q", text="t", citations=[sources[0]], sources=sources, refused=False
    )
    assert ask.chunk_notes(chunks, answer) == [
        "source [1], cited",
        "source [2], not cited",
        "source [1], cited",
    ]


def test_main_prints_chunks_answer_and_timing(monkeypatch, capsys):
    chunks = [make_chunk(1, "10.11.05.A-C"), make_chunk(2, "10.11.02"), make_chunk(3, None)]
    monkeypatch.setattr(ask, "embed_query", lambda question: [0.0])
    monkeypatch.setattr(ask, "get_connection", lambda: nullcontext(None))
    monkeypatch.setattr(ask, "search", lambda conn, vector, k: chunks)
    fake_response = SimpleNamespace(
        text="Activate before contact [1].",
        candidates=[],
        usage_metadata=SimpleNamespace(
            prompt_token_count=900, candidates_token_count=20, thoughts_token_count=0
        ),
    )
    monkeypatch.setattr(generate, "call_llm", lambda system, user: fake_response)
    monkeypatch.setattr(sys, "argv", ["ask.py", "When must cameras be on?"])

    ask.main()
    out = capsys.readouterr().out
    assert "1. DGO-10.11 §10.11.05.A-C  distance 0.200  → source [1], cited" in out
    assert "2. DGO-10.11 §10.11.02  distance 0.200  → source [2], not cited" in out
    assert "3. DGO-10.11 (intro)  distance 0.200" in out  # No "§" for intro text.
    assert "Activate before contact [1].\n\nThis is not legal advice." in out
    assert "Timing: embed" in out and "Tokens: 900 in / 20 out" in out
    assert out.rstrip().endswith(generate.DISCLAIMER)
