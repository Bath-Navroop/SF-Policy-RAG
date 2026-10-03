"""Tests for app/generate.py. None of them call the Gemini API or need the database."""

from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors

from app import generate
from app.generate import (
    REFUSAL,
    build_prompt,
    build_sources,
    is_refusal,
    parse_citations,
    section_label,
    source_label,
)


def make_chunk(chunk_id, section_path, chunk_index, content="text", document_id="DGO-10.11"):
    """A chunk shaped like what app.retrieval.search() returns."""
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "doc_title": "Body Worn Cameras",
        "section_path": section_path,
        "section_title": f"{section_path[:8]} ACTIVATION" if section_path else None,
        "chunk_index": chunk_index,
        "content": content,
        "effective_date": date(2024, 10, 19),
        "revised_date": date(2024, 9, 4),
        "source_url": "https://example.org/10-11",
        "distance": 0.2,
    }


def test_sources_numbered_in_rank_order():
    chunks = [make_chunk(7, "10.11.05.A-C", 4), make_chunk(3, "10.11.02", 1)]
    sources = build_sources(chunks)
    assert [(s["n"], s["section_path"]) for s in sources] == [(1, "10.11.05.A-C"), (2, "10.11.02")]


def test_repeated_section_becomes_one_source_in_document_order():
    # Ranks 1 and 3 are two windows of the same long item, retrieved out of order.
    chunks = [
        make_chunk(12, "10.11.05.E", 9, "second window"),
        make_chunk(3, "10.11.02", 1),
        make_chunk(11, "10.11.05.E", 8, "first window"),
    ]
    sources = build_sources(chunks)
    assert len(sources) == 2
    assert sources[0]["chunk_ids"] == [11, 12]
    assert sources[0]["content"] == "first window\n[...]\nsecond window"


def test_same_section_path_in_different_documents_stays_separate():
    chunks = [make_chunk(1, None, 0), make_chunk(2, None, 0, document_id="DGO-5.01")]
    assert len(build_sources(chunks)) == 2


@pytest.mark.parametrize(
    "path, heading, expected",
    [
        # Every heading style below appears in the real data (all 113 orders checked).
        ("10.11.05.A-C", "10.11.05 ACTIVATION OF BWC", "§10.11.05.A-C ACTIVATION OF BWC"),
        ("6.09.01", "6.09.01. PURPOSE", "§6.09.01 PURPOSE"),
        ("6.14.03.A", "6.14.03LEGAL STANDARDS", "§6.14.03.A LEGAL STANDARDS"),
        ("III.B", "III. PROCEDURES", "§III.B PROCEDURES"),
        ("II", "II.PROCEDURES", "§II PROCEDURES"),
        ("III.A", "Ill. PROCEDURES", "§III.A PROCEDURES"),  # Typo on the page: l for I.
        ("IV.A", "IV. DUTY EVALUATION COMMITTEE.", "§IV.A DUTY EVALUATION COMMITTEE"),
        ("III.F", "III. PROCEDURES:", "§III.F PROCEDURES"),
        ("A", "III PROCEDURES", "§III.A PROCEDURES"),  # DGO 6.08: path lacks the number.
        ("10.02.01", "INVESTIGATIONS", "§10.02.01 INVESTIGATIONS"),  # Not a roman numeral.
        (None, None, ""),  # Intro text before the first heading.
    ],
)
def test_section_label_handles_every_heading_style(path, heading, expected):
    assert section_label(path, heading) == expected


def test_source_label_falls_back_to_revised_date():
    source = build_sources([make_chunk(1, "10.11.05.A-C", 0)])[0]
    assert source_label(source) == (
        "DGO 10.11 Body Worn Cameras, §10.11.05.A-C ACTIVATION (effective 2024-10-19)"
    )
    source["effective_date"] = None
    assert source_label(source).endswith("(revised 2024-09-04)")


def test_prompt_has_numbered_sources_then_question():
    sources = build_sources([make_chunk(1, "10.11.05.A-C", 0, "Members shall activate.")])
    prompt = build_prompt("When must cameras be on?", sources)
    assert "[1] DGO 10.11 Body Worn Cameras" in prompt
    assert "Members shall activate." in prompt
    assert prompt.endswith("Question: When must cameras be on?")


@pytest.mark.parametrize(
    "text, valid, invalid",
    [
        ("Cameras must be on [1].", [1], []),
        ("Both apply [2][1] and again [1].", [1, 2], []),  # Repeats counted once, sorted.
        ("See [1, 3] and [2,3].", [1, 2, 3], []),
        ("Made up [7].", [], [7]),  # Only 3 sources exist.
        ("No citations here. Section 10.11.05 [a] too.", [], []),
    ],
)
def test_parse_citations(text, valid, invalid):
    sources = [{"n": n} for n in (1, 2, 3)]
    cited, bad = parse_citations(text, sources)
    assert [s["n"] for s in cited] == valid
    assert bad == invalid


def test_intro_source_label_has_no_section():
    source = build_sources([make_chunk(1, None, 0)])[0]
    assert source_label(source) == "DGO 10.11 Body Worn Cameras (effective 2024-10-19)"


def test_is_refusal_ignores_case_and_curly_apostrophes():
    assert is_refusal(REFUSAL)
    assert is_refusal("i couldn’t find this in the policies i have. They cover X.")
    assert not is_refusal("Officers must activate the camera [1].")


def test_no_chunks_refuses_without_calling_the_llm(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("the LLM should not be called")

    monkeypatch.setattr(generate, "call_llm", fail)
    answer = generate.generate("Anything?", [])
    assert answer.refused and answer.text == REFUSAL and answer.citations == []


def test_generate_end_to_end_with_fake_llm(monkeypatch):
    fake_response = SimpleNamespace(
        text="Officers must activate the BWC before a stop [1]. Also [4].",
        candidates=[],
        usage_metadata=SimpleNamespace(
            prompt_token_count=900, candidates_token_count=40, thoughts_token_count=0
        ),
    )
    monkeypatch.setattr(generate, "call_llm", lambda system, user: fake_response)

    answer = generate.generate("When?", [make_chunk(1, "10.11.05.A-C", 0)])
    assert [s["section_path"] for s in answer.citations] == ["10.11.05.A-C"]
    assert answer.invalid_citations == [4]
    assert not answer.refused
    assert (answer.input_tokens, answer.output_tokens) == (900, 40)


class FakeModels:
    """Stands in for client.models: raises each queued error in turn, then answers."""

    def __init__(self, failures):
        self.failures = list(failures)
        self.configs = []

    def generate_content(self, model, contents, config):
        self.configs.append(config)
        if self.failures:
            raise self.failures.pop(0)
        return "fake response"


def use_fake_client(monkeypatch, failures):
    models = FakeModels(failures)
    monkeypatch.setattr(generate, "get_client", lambda: SimpleNamespace(models=models))
    waits = []
    monkeypatch.setattr(generate.time, "sleep", waits.append)  # Record waits, don't sleep.
    return models, waits


def test_call_llm_retries_timeouts_and_server_errors(monkeypatch):
    busy = errors.ServerError(503, {"error": {"message": "high demand"}})
    models, waits = use_fake_client(monkeypatch, [httpx.ReadTimeout("slow"), busy])
    assert generate.call_llm("rules", "question") == "fake response"
    assert len(models.configs) == 3  # Two failures, then success.
    assert waits == [5, 15]


def test_call_llm_gives_up_after_max_attempts(monkeypatch):
    use_fake_client(monkeypatch, [httpx.ReadTimeout("slow")] * 3)
    with pytest.raises(httpx.ReadTimeout):
        generate.call_llm("rules", "question")


def test_call_llm_does_not_retry_a_bad_request(monkeypatch):
    bad = errors.ClientError(400, {"error": {"message": "bad request"}})
    models, waits = use_fake_client(monkeypatch, [bad])
    with pytest.raises(errors.ClientError):
        generate.call_llm("rules", "question")
    assert len(models.configs) == 1 and waits == []


def test_call_llm_sets_timeout_and_disables_function_calling(monkeypatch):
    models, _ = use_fake_client(monkeypatch, [])
    generate.call_llm("rules", "question")
    sent = models.configs[0]
    assert sent.http_options.timeout == generate.REQUEST_TIMEOUT_MS
    assert sent.automatic_function_calling.disable is True
