"""Database connection, shared by the ingestion scripts and (later) the API."""

import psycopg

from app import config


def get_connection() -> psycopg.Connection:
    """Open a connection to the Postgres database named by DATABASE_URL in .env.

    autocommit=True means each statement is saved immediately, unless we group
    statements with `with conn.transaction():`, which saves them all or none.
    """
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    return psycopg.connect(config.DATABASE_URL, autocommit=True)


def to_pgvector(values: list[float]) -> str:
    """Format a vector the way Postgres/pgvector reads it as text: '[0.1,0.2,...]'."""
    return "[" + ",".join(str(value) for value in values) + "]"
