-- SF Police Policy Explorer -- database schema
-- Embedding dimension: 3072 (Gemini Embedding 1/2, the plan's chosen default).
-- If you switch to OpenAI text-embedding-3-small instead, this must be 1536 --
-- and every existing chunk re-embedded, not just an env var change.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
  id              TEXT PRIMARY KEY,          -- e.g. 'DGO-10.11'
  title           TEXT NOT NULL,
  source_type     TEXT NOT NULL,             -- 'dgo' | 'bulletin' | 'police_code'
  source_url      TEXT NOT NULL,
  effective_date  DATE,                      -- NULL if the listing only shows a revised date
  revised_date    DATE,
  downloaded_at   TIMESTAMPTZ DEFAULT now()
);

-- For databases created before revised_date existed (CREATE TABLE IF NOT EXISTS
-- won't change an existing table). Makes this whole file safe to re-run.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS revised_date DATE;

CREATE TABLE IF NOT EXISTS chunks (
  id            BIGSERIAL PRIMARY KEY,
  document_id   TEXT REFERENCES documents(id) ON DELETE CASCADE,
  section_path  TEXT,                        -- e.g. '10.11.05', '10.11.05.B-D' or 'III.B'
  section_title TEXT,
  page_number   INT,
  chunk_index   INT NOT NULL,
  content       TEXT NOT NULL,
  embedding     vector(3072),
  tsv           tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);

-- No index on `embedding`: pgvector's HNSW/IVFFlat indexes cap out at 2000
-- dimensions, and Gemini's embeddings are 3072-dim -- over that limit. At this
-- project's scale (roughly 1,000-2,000 chunks), a plain sequential scan for
-- cosine distance is fast enough that an approximate-nearest-neighbor index
-- wouldn't be noticeable; revisit only if the corpus grows into the hundreds
-- of thousands of rows.
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv);

CREATE TABLE IF NOT EXISTS queries (
  id             BIGSERIAL PRIMARY KEY,
  question       TEXT NOT NULL,
  answer         TEXT,
  chunk_ids      BIGINT[],
  latency_ms     INT,
  input_tokens   INT,
  output_tokens  INT,
  cost_usd       NUMERIC(10,6),
  feedback       SMALLINT,                   -- 1 = thumbs up, -1 = down, NULL = none
  created_at     TIMESTAMPTZ DEFAULT now()
);

-- Supabase automatically exposes tables in the public schema through its REST API (the
-- "Data API"), where anyone with the project's public key could read or write them. This
-- app connects to Postgres directly and never uses that API, so it's switched off in the
-- Supabase dashboard. Row Level Security with no policies is a second lock: the API's
-- roles can't see or change any row. The app connects as the tables' owner, which RLS
-- doesn't apply to, so this changes nothing for the app or the local database.
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE queries ENABLE ROW LEVEL SECURITY;
