"""Turning text into embedding vectors with Gemini.

Used by ingest/embed.py (for chunks) and, later, by the retriever (for questions).

An embedding is a list of numbers (3,072 of them here) that captures what a text
means. Texts with similar meanings get vectors that point in similar directions,
which is how we'll find the chunks closest to a question.
"""

from google import genai
from google.genai import types

from app import config

_client: genai.Client | None = None


def get_client() -> genai.Client:
    """Create the Gemini client the first time it's needed, then reuse it."""
    global _client
    if _client is None:
        if not config.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set in .env.")
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def embed(texts: list[str], task_type: str) -> list[list[float]]:
    """Embed a batch of texts in one API call. Returns one vector per text, in order.

    task_type tells the model how the vector will be used. Documents and questions
    are embedded slightly differently ("RETRIEVAL_DOCUMENT" vs "RETRIEVAL_QUERY"),
    which helps a short question match the passage that answers it.
    """
    if "embedding-2" in config.EMBED_MODEL:
        # gemini-embedding-2 doesn't accept task_type and needs different batching;
        # supporting it is a Weeks 9-10 experiment, not something to switch silently.
        raise RuntimeError(f"{config.EMBED_MODEL} isn't supported yet; use gemini-embedding-001.")

    result = get_client().models.embed_content(
        model=config.EMBED_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(task_type=task_type),
    )
    vectors = [embedding.values for embedding in result.embeddings]

    # Fail loudly rather than store something the database or search can't use.
    if len(vectors) != len(texts):
        raise RuntimeError(f"Asked for {len(texts)} embeddings, got {len(vectors)}.")
    for vector in vectors:
        if len(vector) != config.EMBED_DIM:
            raise RuntimeError(
                f"Got {len(vector)}-number vectors, but the database expects {config.EMBED_DIM}."
            )
    return vectors


def embed_documents(texts: list[str]) -> list[list[float]]:
    return embed(texts, "RETRIEVAL_DOCUMENT")


def embed_query(text: str) -> list[float]:
    return embed([text], "RETRIEVAL_QUERY")[0]
