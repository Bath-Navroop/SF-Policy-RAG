"""Tests for the FastAPI app (app/main.py). No API calls and no database: the pipeline
and the database connection are replaced with fakes, so these run anywhere (incl. CI)."""

from datetime import date

import psycopg
import pytest
from fastapi.testclient import TestClient
from google.genai import errors

from app import generate, main


class FakeConnection:
    """Stands in for a psycopg connection: records every SQL statement it's given."""

    def __init__(self, rowcount=1, fail_on_insert=False):
        self.statements = []  # (sql, params) pairs, in order
        self.rowcount = rowcount
        self.fail_on_insert = fail_on_insert

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self.fail_on_insert and "INSERT" in sql:
            raise psycopg.errors.UndefinedTable("relation queries does not exist")
        self.statements.append((sql, params))
        return self  # psycopg returns a cursor; we only need fetchone() and rowcount from it.

    def fetchone(self):
        return (42,)  # The id Postgres would give the new queries row.


def make_answer(question, text="Activate before arrival [1].", refused=False):
    chunks = [
        {
            "chunk_id": chunk_id,
            "document_id": "DGO-10.11",
            "doc_title": "Body Worn Cameras",
            "section_path": path,
            "section_title": "10.11.05 ACTIVATION",
            "chunk_index": 0,
            "content": "Members shall activate the BWC.",
            "effective_date": date(2025, 11, 6),
            "revised_date": None,
            "source_url": "https://example.org/10-11",
            "distance": 0.2,
        }
        for chunk_id, path in [(7, "10.11.05.A-C"), (8, "10.11.02")]
    ]
    sources = generate.build_sources(chunks)
    return generate.Answer(
        question=question,
        text=text,
        citations=[sources[0]],  # Only [1] is cited.
        sources=sources,
        refused=refused,
        model="gemini-test",
        input_tokens=1000,
        output_tokens=200,
        thinking_tokens=5,
    )


@pytest.fixture
def conn(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(main, "get_connection", lambda: connection)
    return connection


@pytest.fixture
def pipeline_calls(monkeypatch):
    """Replace the real pipeline (embedding + search + Gemini) with a fake one."""
    calls = []

    def fake_answer_question(conn, question):
        calls.append(question)
        return make_answer(question)

    monkeypatch.setattr(main, "answer_question", fake_answer_question)
    return calls


@pytest.fixture(autouse=True)
def reset_rate_limits():
    # The limiter keeps its counters in memory; without this, requests from one test
    # would count against the next.
    main.limiter.reset()


BODY = {"question": "Body cameras?"}  # A valid /ask request body.


def visitor(ip="203.0.113.1"):
    """A test client that looks like it's connecting from `ip`."""
    return TestClient(main.app, client=(ip, 50000))


# ---------- /ask ----------


def test_ask_returns_answer_citations_and_query_id(conn, pipeline_calls):
    response = visitor().post("/ask", json={"question": "  When must cameras be on?  "})

    assert response.status_code == 200
    data = response.json()
    assert data["answer"] == "Activate before arrival [1].\n\nThis is not legal advice."
    assert data["refused"] is False
    assert data["query_id"] == 42
    assert data["disclaimer"] == generate.DISCLAIMER
    assert data["citations"] == [
        {
            "n": 1,
            "document_id": "DGO-10.11",
            "title": "Body Worn Cameras",
            "section": "§10.11.05.A-C ACTIVATION",
            "date": "effective 2025-11-06",
            "url": "https://example.org/10-11",
        }
    ]
    assert pipeline_calls == ["When must cameras be on?"]  # Whitespace trimmed.


def test_ask_logs_the_query_without_the_legal_note_or_ip(conn, pipeline_calls):
    visitor("198.51.100.9").post("/ask", json={"question": "When must cameras be on?"})

    [(sql, params)] = conn.statements
    assert "INSERT INTO queries" in sql
    question, answer, chunk_ids, latency_ms, input_tokens, output_tokens, cost = params
    assert question == "When must cameras be on?"
    assert answer == "Activate before arrival [1]."  # The model's words only.
    assert chunk_ids == [7, 8]  # Every retrieved chunk, cited or not.
    assert latency_ms >= 0
    assert (input_tokens, output_tokens, cost) == (1000, 205, 0)  # Thinking counts as output.
    assert "198.51.100.9" not in str(params)  # PLAN.md Section 9: never store IPs.


@pytest.mark.parametrize("question", ["", "  ", "hi", "x" * 501])
def test_ask_rejects_bad_questions_without_calling_the_pipeline(conn, pipeline_calls, question):
    response = visitor().post("/ask", json={"question": question})
    assert response.status_code == 422
    assert pipeline_calls == []


def test_ask_rejects_a_missing_question(conn, pipeline_calls):
    assert visitor().post("/ask", json={}).status_code == 422


def test_per_visitor_rate_limit(conn, pipeline_calls):
    client = visitor()
    codes = [client.post("/ask", json=BODY).status_code for _ in range(11)]
    assert codes == [200] * 10 + [429]
    assert "wait a minute" in client.post("/ask", json=BODY).json()["detail"]
    # Another visitor isn't affected by the first one's limit.
    assert visitor("203.0.113.2").post("/ask", json=BODY).status_code == 200


def test_site_wide_rate_limit(conn, pipeline_calls):
    # 14 questions from 3 visitors (each under their own limit of 10) use up the
    # site-wide 14/minute, so a fourth visitor is refused on their first question.
    for ip, count in [("10.0.0.1", 5), ("10.0.0.2", 5), ("10.0.0.3", 4)]:
        client = visitor(ip)
        for _ in range(count):
            assert client.post("/ask", json=BODY).status_code == 200
    assert visitor("10.0.0.4").post("/ask", json=BODY).status_code == 429


def test_gemini_still_busy_after_retries_gives_friendly_503(conn, monkeypatch):
    def busy(conn, question):
        raise errors.ServerError(503, {"error": {"message": "high demand"}})

    monkeypatch.setattr(main, "answer_question", busy)
    response = visitor().post("/ask", json=BODY)
    assert response.status_code == 503
    assert response.json()["detail"] == main.BUSY_MESSAGE


def test_other_gemini_errors_give_500(conn, monkeypatch):
    def bad_request(conn, question):
        raise errors.ClientError(400, {"error": {"message": "bad request"}})

    monkeypatch.setattr(main, "answer_question", bad_request)
    response = visitor().post("/ask", json=BODY)
    assert response.status_code == 500
    assert "bad request" not in response.text  # Internal details stay in the server log.


def test_database_down_gives_friendly_503(pipeline_calls, monkeypatch):
    def no_database():
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(main, "get_connection", no_database)
    response = visitor().post("/ask", json=BODY)
    assert response.status_code == 503
    assert response.json()["detail"] == main.DB_DOWN_MESSAGE


def test_answer_still_returned_if_logging_fails(pipeline_calls, monkeypatch):
    monkeypatch.setattr(main, "get_connection", lambda: FakeConnection(fail_on_insert=True))
    response = visitor().post("/ask", json=BODY)
    assert response.status_code == 200
    assert response.json()["query_id"] is None  # The page hides the thumbs.


# ---------- /feedback ----------


@pytest.mark.parametrize("rating", [1, -1])
def test_feedback_saves_the_rating(conn, rating):
    response = visitor().post("/feedback", json={"query_id": 42, "rating": rating})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    [(sql, params)] = conn.statements
    assert sql.startswith("UPDATE queries SET feedback")
    assert params == (rating, 42)


@pytest.mark.parametrize("rating", [0, 2, "up"])
def test_feedback_rejects_other_ratings(conn, rating):
    assert visitor().post("/feedback", json={"query_id": 42, "rating": rating}).status_code == 422
    assert conn.statements == []


def test_feedback_for_unknown_query_is_404(monkeypatch):
    monkeypatch.setattr(main, "get_connection", lambda: FakeConnection(rowcount=0))
    assert visitor().post("/feedback", json={"query_id": 999, "rating": 1}).status_code == 404


# ---------- /health and the web page ----------


def test_health_checks_the_database(conn):
    response = visitor().get("/health")
    assert response.json() == {"status": "ok"}
    assert conn.statements == [("SELECT 1", None)]


def test_health_reports_database_down(monkeypatch):
    def no_database():
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(main, "get_connection", no_database)
    assert visitor().get("/health").status_code == 503


def test_home_page_is_served():
    response = visitor().get("/")
    assert response.status_code == 200
    assert "SF Police Policy Explorer" in response.text
    assert visitor().get("/app.js").status_code == 200
