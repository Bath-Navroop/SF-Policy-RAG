"""Settings read from environment variables (the .env file). One place to see every setting."""

import os

from dotenv import load_dotenv

load_dotenv()  # Copies the values in .env into environment variables (if .env exists).

DATABASE_URL = os.environ.get("DATABASE_URL", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Checked against the Gemini docs (ai.google.dev/gemini-api/docs/embeddings) on 2026-09-27.
EMBED_MODEL = os.environ.get("EMBED_MODEL", "gemini-embedding-001")
# Flash Lite only (free tier: 500 requests/day; non-Lite Flash models get 20/day).
# Model ID checked against ai.google.dev/gemini-api/docs/models on 2026-09-28.
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.1-flash-lite")

# Must match `vector(3072)` in sql/schema.sql. Changing it means re-embedding everything.
EMBED_DIM = 3072
