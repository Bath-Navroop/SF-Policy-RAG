"""Database connection, shared by the ingestion scripts and (later) the API."""

import os

import psycopg
from dotenv import load_dotenv

load_dotenv()  # Copies the values in .env into environment variables (if .env exists).


def get_connection() -> psycopg.Connection:
    """Open a connection to the Postgres database named by DATABASE_URL in .env.

    autocommit=True means each statement is saved immediately, unless we group
    statements with `with conn.transaction():`, which saves them all or none.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    return psycopg.connect(url, autocommit=True)
