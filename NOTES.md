# SF Police Policy Explorer — Build Notes & Decisions Log

> **Purpose of this file:** the history behind `PLAN.md` — what was built, how, what was measured, what went wrong, and why each decision was made. `PLAN.md` says what to do next; this file says what happened. It's also the raw material for the README's "Key decisions and tradeoffs" section and for interview answers.
>
> **Created:** 2026-10-03 by splitting the original PLAN.md. Add new entries to the relevant section, dated.

---

## 1. Timeline

| Date | Milestone |
|---|---|
| 2026-09-23 | Plan written |
| 2026-09-24 | Free-tier quotas confirmed from Nav's AI Studio dashboard; Neon considered, Supabase kept |
| 2026-09-26 | Week 0 complete. Discovered DGOs are HTML pages, not PDFs → BeautifulSoup + `5.01.03`-style section numbers. Decided to stay with Gemini embeddings (local embeddings deferred). Schema applied with no vector index |
| 2026-09-27 | Weeks 1–2 complete: 113 documents, 857 chunks in local Postgres. All 857 chunks embedded (`gemini-embedding-001`). `app/retrieval.py` done (5/5 example questions right order at #1). **Working style changed:** the assistant writes the code; Nav learns by reading and asking questions |
| 2026-09-28 | Decided rules go in the system instruction, sources + question in the user message. Retrieval CLI cosmetic fix |
| 2026-10-02 | `app/generate.py` done; cited answers work end to end; LLM model ID `gemini-3.1-flash-lite` verified |
| 2026-10-03 | Legal note moved from prompt to code. `ask.py` done. Weeks 3–4 complete. `chunk.py` section-path oddities deferred as a known limitation. PLAN.md split into PLAN.md + NOTES.md |

---

## 2. Data source findings — DGO pages

Checked 2026-09-26 / 2026-09-27.

- **No PDFs.** Each order has its own HTML page (e.g. `https://www.sanfranciscopolice.org/your-sfpd/policies/general-orders/10-11`) with the full policy text on the page. PDF copies are only available via a public records request, so `pypdf` isn't needed for DGOs (kept installed in case Phase 2 bulletins are PDFs).
- **Listing page:** all orders on one page, no pagination, grouped by category. Each entry shows dates like `(Revised 9/4/24)(Effective 10/19/24)` — the effective date is parsed from there with a regex.
- **URL slugs aren't always clean** (e.g. Community Policing is `/general-orders/1-08-0`), so `doc_id` (`DGO-1.08`) is built from the order number in the link text, not the slug.
- **Policy text** is always in `#policy-content div.field--name-body`.
- **Headings** are `<h2>`/`<h3>` tags (the plan originally guessed bold paragraphs) in two styles: new `10.11.05 TITLE` and old `III. TITLE` (44 orders). Sub-items are numbered `1.`, `2.` with lettered `a.`, `b.` below them.
- **Sub-item labels (A./1./a.) are drawn by CSS, not in the text,** so the parser rebuilds them. Verified: 10.11.05 items A–E match the text's own reference to "10.11.05 C".
- **Tables** are rendered one row per line with every value labelled by its column header.
- **DGO 6.16** wraps sections inside a `<p>` (handled).
- **robots.txt:** only admin, login, search and similar Drupal paths are disallowed; no crawl-delay. General Orders pages are allowed. Nav read the site's terms of use (2026-09-27) before the full download.
- **Some orders are listed twice** — an old version and a newer one at a URL ending in `-0`: 5.08, 5.20, 5.23, 6.13, 6.16, 8.12 (e.g. 5.08 "Non-Uniformed Officers", revised 1996, vs. 5.08 "Plainclothes, Non-Uniformed, and Undercover Officers", effective 2026-05-21). `download.py` keeps only the newest version by effective date (falling back to revised date) and prints a note for each. Caught by questioning why there were 113 orders instead of ~70.
- **Source-page typos kept as-is:** DGO 1.06 has `1.061.01`; DGO 11.10 uses `11.07.xx` numbers; one page has `Ill.` for `III.`.

---

## 3. Ingestion as built (Weeks 1–2, 2026-09-27)

**`ingest/download.py`** — scrapes the listing for `/general-orders/` links; downloads to `data/raw/DGO-x.xx.html` (skips files already saved; User-Agent; 1–2 s delay); writes `data/manifest.csv` (113 rows). Pages were explored by hand first (Inspect + `httpx`/`BeautifulSoup` in `uv run python`).

**`ingest/parse.py`** — writes `data/parsed/DGO-x.xx.json` (gitignored) with one entry per section (`number`, `heading`, `text`). `--print` outputs readable Markdown (redirect to a `.md` file to preview in VS Code). Nav inspected 10.11, 5.21 and 5.01 by eye; table cells and header labels were fixed along the way. Leftover: 6.08's `III PROCEDURES` heading (no dot) gets no number; text is kept. `ingest/__init__.py` added so modules run with `python -m`.

**`ingest/chunk.py`** — sections come from `parse.py`'s HTML headings, so no heading regex is needed; top-level items are detected per line. Doesn't write files; `load.py` imports it.
1. A section ≤ 400 words is one chunk.
2. A longer section is split into units — each top-level item (A., B., 1. …) with everything nested under it, or a plain paragraph/table row — and neighbouring units are packed up to 400 words without cutting a unit.
3. A single unit over 400 words is cut into word windows with 50-word overlap (line breaks kept).
- Section paths: `10.11.05` (whole section), `10.11.05.B` (one item), `10.11.05.A-C` (group of items). The original plan proposed `5.01.03.2.a` for sub-items; not used.
- The context prefix is added only by `embedding_text()` at embedding time, not stored in `content`.
- **Result:** 857 chunks, 5–400 words (median 221). 94 chunks under 40 words (mostly one-line purpose statements) kept as whole sections for now.
- `tests/test_chunk.py`, 9 tests: short section = 1 chunk, nested lines stay with their item, splits between items not mid-item, max size respected, overlap correct, line breaks kept, metadata + indexes, embedding prefix. `pyproject.toml` has `[tool.pytest.ini_options] pythonpath = ["."]` so tests can import `ingest`.

**`ingest/load.py` + `app/db.py`** — `get_connection()` reads `DATABASE_URL` from `.env`, autocommit + explicit transactions. Each order loads in its own transaction (upsert document, replace chunks); orders whose chunks are unchanged are skipped, so re-running never wipes embeddings. `sql/schema.sql` gained `documents.revised_date` plus an `ALTER TABLE … ADD COLUMN IF NOT EXISTS` so the file is safe to re-run.

**Done check:** `SELECT document_id, section_path, left(content, 80) FROM chunks LIMIT 20;` showed clean chunks (confirmed by Nav). psql tips: add `ORDER BY document_id, chunk_index` (tables have no built-in order); `-P pager=off` avoids the pager, or press `q`.

---

## 4. Embeddings and vector storage

### Model choice
- **2026-09-26: stay with Gemini** rather than local embeddings. `sentence-transformers` models (`all-MiniLM-L6-v2`, 384 dims; `all-mpnet-base-v2`, 768 dims) run on the MacBook's CPU with no API limits or cost, but are likely lower quality and add PyTorch as a heavy dependency. Kept as a Weeks 9–10 experiment so evals measure the difference.
- **2026-09-27: `gemini-embedding-001`, not `gemini-embedding-2`.** Per the Gemini embeddings docs, `-2` doesn't accept `task_type` (uses text prefixes like `title: … | text: …` instead) and returns ONE combined embedding for a plain list of texts unless each is wrapped in its own `Content`. `-001` is text-only, supports `RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY`, has a 2,048-token input limit and 3,072 dims. The plan's old ID `gemini-embedding-1` isn't a real model ID (fixed in `.env.example`).
- OpenAI `text-embedding-3-small` (1,536 dims, ~$0.02/1M tokens) considered — cheaper per token if paid, but no free tier.

### Corpus size (measured 2026-09-27)
113 DGOs, 484 sections, ~181K words (~1.18M chars). Estimated ~790 chunks at 400 words (actual: 857) ≈ ~330K tokens including overlap and prefixes (~415 tokens/chunk). Other sizes: 200-word chunks ≈ 1,380; 800-word ≈ 590. Replaced an earlier ~790K-token guess.

### Free-tier request counting (measured on the first full run, 2026-09-27)
**Every text counts as one request toward RPM and RPD, even when 40 texts are sent in one API call.** After ~860 chunks the AI Studio dashboard showed RPD 805/1K, RPM 80/100 (two 40-chunk batches in one minute) and TPM 28.26K/30K — closer to the cap than intended, because averaging the rate isn't the same as staying under it in every 60-second window.

### `ingest/embed.py` + `app/embeddings.py` + `app/config.py`
- `app/config.py`: all settings from `.env` in one place, incl. `EMBED_DIM = 3072`; holds no secrets itself.
- `app/embeddings.py`: `embed_documents()` (`RETRIEVAL_DOCUMENT`), `embed_query()` (`RETRIEVAL_QUERY`); checks one 3,072-number vector per text.
- `ingest/embed.py`: only `WHERE embedding IS NULL`; each batch saved as it finishes (resumable); retries 429/500/503 with 30/60/120/240 s backoff.
- **After the first run:** batches of 30 and a sliding-window `RateLimiter` keeping any 60 s window under 70 requests and 26K estimated tokens (simulated worst window: 60 requests / ~24.8K tokens). ~60 chunks/min ≈ 15 min for a full run; warns when a run will use most of the daily 1,000 requests.
- First full run: all 857 chunks, ~15 min, no errors.

### Vector column and index (decided 2026-09-26)
- Applying the original schema failed: `column cannot have more than 2000 dimensions for hnsw index` — pgvector's HNSW and IVFFlat indexes cap at 2,000 dims.
- Chose `vector(3072)` with **no index**:
  - **Accuracy:** a sequential scan is exact, always the true top-k; HNSW is approximate.
  - **Speed:** ~10–15 ms on 2,000 synthetic 3,072-dim vectors (~4 ms measured on the real 857) — tiny next to the embedding call (hundreds of ms) and the LLM call (1–3 s+).
  - **Quality:** full dimensions keep the most quality. Google reports shortened embeddings (1,536 / 768) lose only a little, but the trade buys nothing here.
- **Storage:** a 3,072-dim vector is ~12 KB; ~790 chunks ≈ 10 MB, 2,000 ≈ 27 MB — well within Supabase's 500 MB.
- **If the corpus grows:** `halfvec(3072)` + HNSW (all dims, ~half the storage) or 1,536 dims if evals show no loss.

---

## 5. Retrieval as built (2026-09-27)

`app/retrieval.py`: `search()` takes a vector, `retrieve()` embeds the question first (`RETRIEVAL_QUERY`) → `ORDER BY embedding <=> query LIMIT 5` (exact scan, ~4 ms). Returns chunk + everything a citation needs (doc id/title, section path/title, effective & revised dates, source URL, distance, `chunk_index`). `to_pgvector()` moved to `app/db.py` (code in `app/` must not import from `ingest/`). Each question costs 1 embedding request.

**Results on the Section 1 example questions:** body cameras → 10.11.05.A-C (#1); pursuits → 5.05.02 then 5.05.05.A; complaints → 2.04.03.A, 2.04.01; de-escalation → 5.01.04.C (+ related 5.24 Disengagement at #4); dispersal orders → 8.03.03.D.

**Finding — distance doesn't detect unanswerable questions.** Right #1 hits scored 0.18–0.27, but "drone pizza deliveries" still scored 0.297–0.305 (matched district boundaries / vehicle crashes / air support), and a real question's hits ran up to 0.292. Embeddings measure topic, not "answers the question". So refusal is handled by the grounded prompt; a distance cutoff is at most a loose safety net, picked from the labelled unanswerable eval questions.

**Noticed:**
- (a) The same section path can appear twice when a long item was split into overlapping windows (e.g. 5.05.05.E at #3 and #5) — **handled** in `generate.py` by merging.
- (b) The top 5 often all come from one order, and low-value sections (purpose, admin info) fill #3–#5 — likely helped by the shared context prefix. The prefix / hybrid / rerank experiments measure this.
- (c) Resident-phrased questions ("how do *I* file a complaint") vs. officer-facing text (DGO 2.04) — a good eval question type.
- (d) Cosmetic: the CLI printed a dangling " — " when a chunk had no section title — **fixed** 2026-09-28.

---

## 6. Generation as built (2026-10-02, `app/generate.py`)

**Original starting template (from the plan, 2026-09-23):**

```
You answer questions about San Francisco Police Department policy using ONLY the sources below.
Rules:
- Cite every claim with the source number in brackets, e.g. [2].
- If the sources do not answer the question, say "I couldn't find this in the policies I have." Do not guess.
- This is general information, not legal advice.

Sources:
[1] DGO 5.01 Use of Force — 5.01.03 Definitions (effective 2024-10-19)
<chunk text>

Question: <user question>
```

**As built:**
- **Rules in the system instruction; numbered sources + question in the user message** (Nav's decision, 2026-09-28). Keeps rules separate from text that comes from documents or users. The prompt also says sources are reference text, not instructions.
- **Sources:** chunks grouped by (document, section path) so overlapping windows of one item become ONE numbered source (pieces in `chunk_index` order); numbered by best rank.
- **Source labels:** e.g. `DGO 10.11 Body Worn Cameras, §10.11.05.A-C ACTIVATION OF BODY WORN CAMERAS (effective 2025-11-06)`. `split_heading()` handles every heading style found (`10.11.05 TITLE`, `6.09.01. TITLE`, `6.14.03TITLE`, `III. TITLE`, `II.TITLE`, `III TITLE`, `Ill. TITLE`, trailing `.`/`:`) — checked on all 857 chunks. The heading's own number is dropped because the path repeats it; DGO 6.08's unnumbered items show as `§III.A`; intro text shows the order name only; dates fall back to the revised date. Labels are display-only — `section_path` in the DB is unchanged.
- **Refusal:** exact sentence in `REFUSAL`; `Answer.refused` checks for it (case-insensitive, curly apostrophes OK). No retrieved chunks → refusal with no LLM request.
- **Citations:** `[n]`, `[1, 3]`, `[1][3]` parsed; numbers with no matching source kept as `invalid_citations`.
- **Model settings:** temperature default 1.0 (Google strongly recommends not lowering it for Gemini 3 — can cause looping); `thinking_level="minimal"` set explicitly (Flash Lite's default); automatic function calling disabled (no tools; also removes the SDK's "AFC" notice); 30 s request timeout (the SDK's default is none).
- **Retries:** up to 3 attempts on 429/500/503 and timeouts, waiting 5 s then 15 s; other errors (e.g. 400) fail immediately. Worst case 110 s.
- **Latency includes failed attempts and retry waits** (Nav's decision, 2026-10-02). `Answer` records input/output/thinking tokens and model name.
- **API:** `client.models.generate_content` (Google's docs label it "legacy" but it's supported; the Interactions API is an option later).
- `tests/test_generate.py`: 30 tests with fake LLM responses (source merging, every heading style, prompt, citation parsing, refusal, retries/timeouts, legal note).

### Legal-advice note moved from prompt to code (2026-10-03)
- **Now:** every answer's text ends with `LEGAL_NOTE = "This is not legal advice."`, appended in code (`Answer.display_text`). `Answer.text` stays the model's own words so citation parsing, refusal detection and eval judging never see it. The fuller Section 9 footer (`DISCLAIMER`) is still shown with every answer. The prompt only says "explain what the policies say; don't give legal advice or opinions; don't add any disclaimer — the app adds one."
- **Why:** the first version had a conditional prompt rule ("if the question asks for advice about a specific situation, say a lawyer can advise"). It fired unprompted in 2 of 7 runs (complaint, pursuits) and in the pursuits run leaked its own wording ("You can only explain what SFPD policy says"). Nav first chose to keep it and measure it in evals, then (same day) chose a fixed note added by code — no condition to misjudge, nothing to leak, zero tokens.
- **Verified** on the pursuits question: answer ends with exactly the note, no lawyer sentence; same content and citations; input 2,779 tokens vs 2,839 (~60 tokens saved per question).

### First real runs (body-camera question, 2026-10-02)
- Run 1 hit a 503 ("high demand"), retried after 5 s and succeeded — **77 s total**, which led to the 30 s timeout.
- Run 2: 1.6 s, 1,727 input / 404 output / 0 thinking tokens.
- Both correct and cited §10.11.05.A-C; run 2 also cited §10.11.02 (definition of "activate") and §10.11.06.B (activate if sobriety-checkpoint screening leads to further investigation) — Nav checked both against the DGO page: correct.
- **Same prompt, different answer:** identical input (same 1,727 tokens) but run 1 cited 1 source and run 2 cited 3, because temperature is 1.0.

---

## 7. `ask.py` CLI (2026-10-03)

- Calls embed → search → generate as separate steps so each is timed.
- Prints each retrieved chunk with rank, section path, distance, **which source number it became and whether the answer cited it**; then the answer + cited sources; then `Timing: embed · search · LLM · total` (DB connection time excluded — the API will reuse connections; LLM time includes retries) and tokens.
- `generate.py`'s own command line was removed (one way to ask); `app.retrieval` keeps its CLI because it costs no LLM request.
- `tests/test_ask.py`: 2 tests (cited/not-cited notes incl. merged sources; full fake run of `main()`). `uv run pytest` → 41 passed.

---

## 8. Weeks 3–4 results on the example questions (2026-10-03)

Nav checked claims against the DGO pages.

| Question | Retrieval (#1) | Chunks cited / retrieved | Result |
|---|---|---|---|
| Body cameras (3 runs) | 10.11.05.A-C | 1–3 of 5 | Run 2 over-generalised the en-route timing rule to all listed incidents (§10.11.05.B limits it to traffic/pedestrian stops and detentions/arrests); `ask.py` run correct but omitted 10.11.06.B's "or the safety of the patient is deemed to be at risk" |
| Vehicle pursuits | 5.05.02 (0.184) | 5 of 5 (4 sources; 5.05.05.E windows merged) | Correct as far as checked; legal-advice line misfired and leaked prompt wording (rule since removed — §6) |
| Complaint (resident-phrased) | 2.04.03.A | 4 of 5 (3 of 4 sources; 2.04.03.A windows merged) | Translated officer-facing text for a resident; said timeline / DPA follow-up weren't in the sources; legal-advice line misfired |
| De-escalation | 5.01.04.C-E | 3 of 5 sources, across DGO 5.01 + 5.24 | Correct — "fulfilled their duty" claim verified against 5.01.02 |
| Crowd control / dispersal | 8.03.03.D-E | 2 of 5 | Correct — Penal Code 726 citation to §8.03.03.A-C verified |
| Drone pizza (unanswerable) | unrelated chunks (0.297–0.304) | 0 | Correct refusal: exact sentence + one line on what the sources cover; `refused` detected; 38 output tokens |

**Lessons for evals:**
1. The same question with the same sources gave answers of three quality levels across runs (temperature 1.0) → run each config more than once.
2. The run-2 error was in a claim citing the *obviously relevant* source [1] and slipped past a check that only looked at unusual citations → the judge must check every claim.
3. Quoted text is cheap to verify automatically — anything in quotation marks should appear word-for-word in the cited source.
4. Retrieved-but-uncited chunks are common (often generic purpose/policy sections, or all 5 from one order) — what the hybrid/rerank experiments measure.

**Latency:** LLM 1.5–7.1 s without retries; one later pursuits run took 10.7 s with no retry (occasional slow calls → p95 matters, UI needs a "still thinking…" state). A 38-token refusal took 5.7 s → Gemini's own variation, not answer length. Embedding ~0.4–0.5 s; search ~20–27 ms; input 1,533–2,839 tokens; output 38–558; thinking 0.

---

## 9. Known limitations

### `chunk.py` section paths in 7 sections (found 2026-10-03; fix deferred by Nav, may revisit)
`chunk.py` decides what's a top-level item by indentation. On some pages the site's HTML nesting is broken, so sub-items come out un-indented and look top-level, and a chunk's path (built from its first and last top-level label) mixes levels.

- **10.01 §I** — uniform classes 2–13 belong under A; an image breaks the list after item 1 → `§I.A-5`, `§I.6-13`.
- **11.06 §IV** — 1–3 belong under I → `§IV.E-3`.
- **2.02.04** — report contents 1–5 belong under D → `§2.02.04.C-3`.
- **8.12.04** — sub-lists under C and D restart at A., B. → letters A B C A B D…, `§8.12.04.A-A`; even plain-looking `8.12.04.B` may be the second B.
- **8.07 §III** — a References line "D. O. T Emergency Response Guidebook" looks like item D → `§III.E-D`.
- **6.15 §III** — A, 11, 12, B, C, D — "11, 12" belong under A; the page also has two 11s.
- **6.09.04** — real structure: intro list 1–6 then A–U, so `§6.09.04.1-A` is confusing but not wrong.

**Impact:** citations still point to the right order, section and URL; only the item suffix can mislead (6 of 857 chunk paths look odd; others in these sections may be quietly ambiguous). Evals use section-level `expected_sections` for these.

**Proposed fix (ready to pick up):** in `chunk_section()`, check whether a section's top-level labels form one clean list (one kind — capitals / numbers / lowercase — no repeats, always increasing). If not, every chunk in that section gets just the section number (e.g. `10.01 §I`). Section-level rather than per-chunk, because per-chunk misses hidden cases (8.12.04's second B, 6.15's 11–12); trade-off: 6.09.04 loses its (valid) item paths.
Steps: read `load.py` to confirm a path change replaces that order's chunks (→ re-embedding those orders; vectors would be identical since the embedding prefix uses the section *title*, not the path) and count the affected chunks; snapshot all 857 paths; change `chunk.py` + tests (mixed kinds, repeats, out of order, clean list unchanged); diff before/after (expect 857 chunks, identical text, only paths in the 7 sections change); `ingest.load --all` → `ingest.embed` → 857/857; one test question on 6.09.04. Nav OK'd re-embedding if needed.

### Long sections with no lettered items (low impact, no fix needed)
In DGO 1.02 §I (9 chunks), 3.02.03 (5), 5.01.07 (2) and 6.10.05 (2), every chunk's path is just the section number, so `build_sources` merges different parts of the same section into one source — citation still accurate, just less precise.

---

## 10. Setup and tooling notes (Week 0, 2026-09-26)

- Accounts: GitHub, Gemini API key (aistudio.google.com — free tier, no card), Render, Supabase.
- MacBook: Homebrew, Python 3.14.7, `uv`, Git (+ SSH key on GitHub), Docker Desktop, VS Code (Python + Ruff extensions).
- Repo: github.com/Bath-Navroop/SF-Policy-RAG, with `PLAN.md`, stub `README.md`, `.gitignore`, `.env.example`.
- `pyproject.toml` written directly instead of via `uv init`: `fastapi uvicorn psycopg[binary] pgvector httpx beautifulsoup4 pypdf google-genai openai python-dotenv slowapi` (+ dev group: `pytest ruff`); `uv.lock` committed.
- `docker-compose.yml`: `pgvector/pgvector:pg16`, port bound to `127.0.0.1` only (so the DB isn't reachable from shared Wi-Fi). Container name `sf-policy-rag-db`, database `sf_policy_rag`.
- `.env.example` holds safe defaults; the real key is only in the gitignored `.env`. Model IDs verified on first use: `gemini-embedding-001` (2026-09-27), `gemini-3.1-flash-lite` (2026-10-02; the old `-preview` ID is shut down).
- Git commits use the GitHub noreply email (`git config --global user.email`), because GitHub blocks pushes that expose a private email.
- **Neon vs Supabase (2026-09-24):** Neon is plain Postgres with no SDK (matches raw SQL), same 500 MB free tier, pgvector, faster wake (~300 ms vs ~1–2 s) but suspends after 5 min idle (Supabase waits about a week), and has database branching. The project doesn't use Supabase's extras. Kept Supabase for now; switching only changes `DATABASE_URL`.
- **LLM choice reasoning:** the generation task is constrained (answer only from sources, cite, refuse), where budget-tier models tend to hold up; the failure modes to watch come more from prompt design and retrieval than model size. A starting assumption to verify with eval numbers (experiments 6–7). The non-Lite Flash models are capped at 20 req/day on this account — too low for evals.

---

## 11. Interview talking points

- **Unanswerable questions:** similarity scores alone didn't separate answerable from unanswerable questions (right hits 0.18–0.27, unanswerable 0.297, a real question's hits up to 0.292), so refusal comes from the grounded prompt, and any cutoff is tuned on labelled unanswerable questions (§5).
- **Prompt rules can misfire:** a conditional "only if asked for legal advice" rule fired unprompted in 2 of 7 runs and once leaked its own wording, so it was removed and the note is appended by code. Anything that must always happen belongs in code, not in the prompt (§6).
- **Run-to-run variation:** identical prompts gave answers citing 1 vs 3 sources at temperature 1.0, which Google recommends keeping for Gemini 3 — so compare averages over repeated runs, not single runs (§6, §8).
- **Hardest-bug candidates:**
  - The free tier counting every embedded text as a request — found from the AI Studio dashboard after the first run, fixed with a sliding-window rate limiter (§4).
  - Gemini's 3,072-dim embeddings exceed pgvector's 2,000-dim index limit, and why dropping the index was the right call at this scale (§4).
  - The downloader silently keeping the 1996 version of orders listed twice, caught by questioning why there were 113 orders instead of ~70 (§2).
  - The first real answer taking 77 s — a Gemini 503 retry plus the SDK having no default timeout, fixed with a 30 s timeout that is retried like a server error (§6).
  - Broken HTML nesting on a few pages producing misleading item paths, and choosing to defer the fix with section-level eval matching (§9).
- **Why Postgres + pgvector:** text, metadata, vectors and full-text search in one DB; exact scan is fast and exact at this size (§4).
