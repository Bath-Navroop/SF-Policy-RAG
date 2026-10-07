"""The web API: FastAPI routes around the existing pipeline, plus the web page.

Run locally from the repo root (database running, GEMINI_API_KEY in .env):
    uv run uvicorn app.main:app --reload
then open http://127.0.0.1:8000 (the page) or http://127.0.0.1:8000/docs (try the API).

Routes:
    POST /ask       {"question": "..."}           -> answer, citations, query_id
    POST /feedback  {"query_id": 12, "rating": 1}  -> {"ok": true}
    GET  /health                                   -> {"status": "ok"} (also checks the DB)
    GET  /                                         -> the web page in web/

Every route is a plain `def`, not `async def`, on purpose: retrieval and the Gemini call
block while they wait on the network. FastAPI runs plain `def` routes in a pool of worker
threads, so one slow answer doesn't stop other people's requests.
"""

import logging
import time
from pathlib import Path
from typing import Annotated, Literal

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from google.genai import errors
from pydantic import BaseModel, StringConstraints
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.db import get_connection
from app.generate import (
    DISCLAIMER,
    RETRYABLE_CODES,
    Answer,
    answer_question,
    date_note,
    section_label,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("app.api")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# ---------- Rate limits ----------
# Gemini's free tier allows 15 LLM requests/minute and 500/day for the WHOLE app, so the
# site-wide limit stays just under that (leaving ~50/day for Nav's own testing), and the
# per-visitor limit stops one person from using it all up.
# The counters are kept in memory: they reset when the server restarts (Render's free tier
# restarts it after 15 idle minutes), which is acceptable for a demo.
PER_VISITOR_LIMIT = "10/minute;50/day"
SITE_WIDE_LIMIT = "14/minute;450/day"
FEEDBACK_LIMIT = "30/minute"

# get_remote_address = the visitor's IP. Behind Render's proxy this is only correct when
# uvicorn runs with --proxy-headers (see the start command in the README). The IP is used
# for counting in memory only; it is never stored or logged.
limiter = Limiter(key_func=get_remote_address)


def whole_site(request: Request) -> str:
    """Rate-limit key shared by every visitor, so the limit applies to the site as a whole."""
    return "whole-site"


app = FastAPI(title="SF Police Policy Explorer", docs_url="/docs", redoc_url=None)
app.state.limiter = limiter  # slowapi looks for the limiter here.


@app.exception_handler(RateLimitExceeded)
async def too_many_requests(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "detail": "This free demo has a limit on how many questions it can answer. "
            "Please wait a minute and try again."
        },
    )


# ---------- Request and response shapes ----------
# Pydantic checks incoming JSON against these. A question that's empty, too short or over
# 500 characters is rejected with a 422 before our code runs. Whitespace is trimmed first.
Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]


class AskRequest(BaseModel):
    question: Question


class Citation(BaseModel):
    n: int
    document_id: str  # e.g. "DGO-10.11"
    title: str  # e.g. "Body Worn Cameras"
    section: str  # e.g. "§10.11.05.A-C ACTIVATION OF BODY WORN CAMERAS"
    date: str  # e.g. "effective 2025-11-06" (or "revised ..." for old orders)
    url: str


class AskResponse(BaseModel):
    answer: str  # The model's answer + the legal note (Answer.display_text).
    refused: bool
    citations: list[Citation]
    disclaimer: str
    query_id: int | None  # None only if logging the question failed.


class FeedbackRequest(BaseModel):
    query_id: int
    rating: Literal[1, -1]  # Thumbs up / thumbs down; anything else is a 422.


# ---------- Logging questions ----------
# PLAN.md Section 9: store the question and answer, never IP addresses or identities.
INSERT_QUERY_SQL = """
    INSERT INTO queries (question, answer, chunk_ids, latency_ms,
                         input_tokens, output_tokens, cost_usd)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    RETURNING id
"""


def log_query(conn: psycopg.Connection, answer: Answer, latency_ms: int) -> int | None:
    """Save one answered question; return its id (the frontend sends it back with feedback).

    A logging failure shouldn't cost the user their answer, so it's caught and logged.
    """
    chunk_ids = [chunk_id for source in answer.sources for chunk_id in source["chunk_ids"]]
    try:
        row = conn.execute(
            INSERT_QUERY_SQL,
            (
                answer.question,
                answer.text,  # The model's own words (without the legal note added by code).
                chunk_ids,
                latency_ms,
                answer.input_tokens,
                # Thinking is billed as output, so count it as output.
                answer.output_tokens + answer.thinking_tokens,
                0,  # Gemini free tier: $0. Fill in real prices if switching to a paid model.
            ),
        ).fetchone()
        return row[0]
    except psycopg.Error:
        logger.exception("Couldn't log the query")
        return None


def to_response(answer: Answer, query_id: int | None) -> AskResponse:
    citations = [
        Citation(
            n=source["n"],
            document_id=source["document_id"],
            title=source["title"],
            section=section_label(source["section_path"], source["section_title"]),
            date=date_note(source),
            url=source["url"],
        )
        for source in answer.citations
    ]
    return AskResponse(
        answer=answer.display_text,
        refused=answer.refused,
        citations=citations,
        disclaimer=DISCLAIMER,
        query_id=query_id,
    )


# ---------- Routes ----------

BUSY_MESSAGE = "The AI service is busy right now. Please try again in a minute."
DB_DOWN_MESSAGE = "The policy database isn't reachable right now. Please try again shortly."


@app.post("/ask", response_model=AskResponse)
@limiter.limit(PER_VISITOR_LIMIT)
@limiter.limit(SITE_WIDE_LIMIT, key_func=whole_site)
def ask(request: Request, body: AskRequest) -> AskResponse:
    # `request` isn't used here, but slowapi needs it in the signature to find the visitor.
    started = time.perf_counter()
    try:
        # One connection per question. Opening one costs a little time (~tens of ms locally,
        # more to a hosted DB) next to the 2-7 s LLM call; a connection pool is a later
        # improvement if timing shows it matters.
        with get_connection() as conn:
            answer = answer_question(conn, body.question)
            latency_ms = round((time.perf_counter() - started) * 1000)
            query_id = log_query(conn, answer, latency_ms)
    except errors.APIError as error:
        # call_llm() has already retried twice; if Gemini is still refusing, tell the user.
        if error.code in RETRYABLE_CODES:
            logger.warning("Gemini still unavailable after retries: %s", error.code)
            raise HTTPException(status_code=503, detail=BUSY_MESSAGE) from error
        logger.exception("Gemini returned an error")
        raise HTTPException(status_code=500, detail="Something went wrong.") from error
    except httpx.TimeoutException as error:
        logger.warning("Gemini timed out after retries")
        raise HTTPException(status_code=503, detail=BUSY_MESSAGE) from error
    except psycopg.OperationalError as error:  # Can't connect, connection dropped, etc.
        logger.exception("Database unavailable")
        raise HTTPException(status_code=503, detail=DB_DOWN_MESSAGE) from error

    logger.info(
        "query_id=%s latency_ms=%s refused=%s sources=%s cited=%s",
        query_id,
        latency_ms,
        answer.refused,
        len(answer.sources),
        len(answer.citations),
    )
    return to_response(answer, query_id)


@app.post("/feedback")
@limiter.limit(FEEDBACK_LIMIT)
def feedback(request: Request, body: FeedbackRequest) -> dict:
    try:
        with get_connection() as conn:
            updated = conn.execute(
                "UPDATE queries SET feedback = %s WHERE id = %s",
                (body.rating, body.query_id),
            ).rowcount
    except psycopg.OperationalError as error:
        logger.exception("Database unavailable")
        raise HTTPException(status_code=503, detail=DB_DOWN_MESSAGE) from error
    if updated == 0:
        raise HTTPException(status_code=404, detail="Unknown query_id.")
    return {"ok": True}


@app.get("/health")
def health() -> dict:
    """Checks the database too, so an outside ping (e.g. a weekly GitHub Action) both
    confirms the site works and keeps Supabase's free database from pausing."""
    try:
        with get_connection() as conn:
            conn.execute("SELECT 1")
    except psycopg.OperationalError as error:
        raise HTTPException(status_code=503, detail="database unreachable") from error
    return {"status": "ok"}


# Mounted LAST: routes above are matched first; everything else is looked up in web/.
# html=True serves web/index.html for "/".
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
