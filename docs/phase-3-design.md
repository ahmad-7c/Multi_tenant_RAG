# Phase 3 — Retrieval & Query Pipeline (Design)

Status: design, not yet implemented. Written 2026-10-16, against the Phase 2
code at `app/` (documents + document_chunks ingested, parent/child, embedded).

Phase 3 turns the chunks Phase 2 produced into answers: a question goes in,
a cited answer grounded only in that org's retrieved content comes out.

---

## 1. The six locked decisions, and what each one actually commits us to

These were handed down as decisions, not options. Restated here with their
downstream consequences, because most of the design work in this phase is
*consequence management*, not choice-making.

| Decision | What it forces |
|---|---|
| Postgres image `pgvector/pgvector:pg16`; tests run against real Postgres, not SQLite | `tests/conftest.py` is rewritten (§10). Schema in tests comes from Alembic, not `create_all`, because the indexes and the generated column only exist in migrations. |
| Child chunks searched via HNSW (cosine) + Postgres full-text (tsvector/GIN), merged with RRF | Migration `0003` drops the ivfflat index from `0002` and adds HNSW + a generated tsvector column + GIN (§4). Raises the tenant-filter recall problem (§5) — the single biggest risk in this phase. |
| Retrieved text is untrusted data; the LLM gets no tools | The answering LLM is called with no tool parameter at all, not an empty allowlist. Retrieved text never reaches the *rewriter*. Citations are produced by retrieval and validated, never trusted from model output (§9). |
| Cache key = `hash(org_id + normalized rewritten question + org.index_version)` | Rewriting happens *before* the cache lookup, so the rewrite LLM call is paid on every request including cache hits. Mitigated additively with a second rewrite-only cache (§8). Adds `organizations.index_version` and a bump on every corpus change. |
| Never hold a DB session open while streaming an LLM response | The streaming endpoint cannot use `Depends(get_db)`, which collides with an existing CLAUDE.md rule. Resolution in §7 — this needs your sign-off. |
| All external providers (embeddings, reranker, LLM) behind small interfaces with fakes for tests | New `app/providers/` package. The fake embedder has to produce *semantically meaningful* vectors, not random ones, or retrieval-ordering tests assert nothing (§6). |

---

## 2. Prerequisite gap: there is still no way to get a token

Phase 2's build log notes it explicitly: no login or registration endpoint
was ever added. `get_current_user()` validates JWTs, but nothing in this
codebase *mints* one — `tests/conftest.py` hand-encodes tokens with
`jwt.encode`.

Every Phase 3 endpoint is authenticated and org-scoped, so this is now
load-bearing rather than cosmetic. Two ways forward:

- **Add `POST /api/v1/auth/login` as the first ticket of Phase 3** (bcrypt
  verify via the already-pinned `passlib`/`bcrypt==4.0.1`, mint a JWT with
  `sub` + `org_id`). Roughly 60 lines plus a test. Recommended — it unblocks
  any manual testing of the query endpoints.
- **Defer it again** and keep minting tokens by hand in tests. Workable, but
  the query pipeline then cannot be exercised by a human through the API at
  all, which is a bad property for the phase where answer quality is the
  whole deliverable.

Flagging rather than deciding: it's adjacent scope, not retrieval scope.

---

## 3. Pipeline shape

```
question
  │
  ├─ 1. rewrite (LLM)            standalone, normalized question
  ├─ 2. cache lookup             hash(org_id + rewritten + index_version) → hit? replay & stop
  ├─ 3. embed (embeddings)       1536-d query vector, same model as ingestion
  ├─ 4. hybrid search (Postgres) HNSW top-50  +  FTS top-50   → RRF → top 25 children
  ├─ 5. rerank (reranker)        top 25 children → top 8 children
  ├─ 6. expand to parents        distinct parent_ids, best child score wins → ≤5 parents
  ├─ 7. grounding gate           top score below floor, or 0 parents → refuse, no LLM call
  ├─ 8. CLOSE THE DB SESSION     everything below runs with no open session
  ├─ 9. answer (LLM, streaming)  parents as delimited data; no tools
  └─ 10. validate + cache        drop invented citation indices; cache assembled answer
```

**Approach taken:** search at the child level, rerank at the child level,
then expand to parents for the LLM.

**Alternative not taken:** rerank the *parents* after expansion. Fewer
rerank inputs (5–8 instead of 25), so cheaper — but a ~500-token parent
contains the matching sentence plus several hundred tokens of unrelated
neighbouring text, which dilutes the cross-encoder signal. Reranking the
125-token children is reranking exactly the span that matched. The expansion
to parents is a pure widening step afterwards, which is the entire point of
parent-child chunking.

**Alternative not taken:** skip reranking for v1 and use RRF order directly.
Would remove a provider and a network hop, but RRF is a rank-fusion
heuristic that knows nothing about the question's semantics — it only knows
two orderings. The reranker is where precision@5 actually comes from. The
interface means it can be swapped for a no-op fake, so it costs nothing in
tests.

Candidate counts (50 / 25 / 8 / 5) go in `app/core/config.py` as settings,
not constants — they're the first thing that gets tuned once there's real
corpus volume, and tuning them should not require a code change.

---

## 4. Schema changes — migration `0003`

### 4a. ivfflat → HNSW

`0002` created `ix_document_chunks_embedding_ivfflat`. It gets dropped.

```sql
DROP INDEX ix_document_chunks_embedding_ivfflat;

CREATE INDEX ix_document_chunks_embedding_hnsw
  ON document_chunks USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
```

`vector_cosine_ops` carries over unchanged — OpenAI embeddings are compared
with cosine (`<=>`), and an operator class that doesn't match the query's
distance operator means the index is silently never used.

`m = 16, ef_construction = 64` are pgvector's defaults, kept deliberately:
they're a reasonable recall/build-time point, and tuning them blind on an
empty table is guessing. `ef_search` is the knob that matters at query time
and it's set per-transaction (§5), not baked into the index.

Why HNSW over ivfflat, now that it's being decided explicitly: ivfflat's
recall depends on `lists` being tuned to the row count, and `0002` admits
`lists = 100` was a cold-table placeholder "to re-tune as row volume grows"
— i.e. it carried a scheduled reindex as technical debt. HNSW needs no such
parameter, has better recall at the same latency, and builds incrementally
as rows arrive. The cost is a larger index and slower build, which for a
corpus that grows by one document at a time is the right trade.

### 4b. Full-text: generated column + partial GIN

```sql
ALTER TABLE document_chunks
  ADD COLUMN content_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX ix_document_chunks_content_tsv
  ON document_chunks USING gin (content_tsv)
  WHERE parent_id IS NOT NULL;
```

**Approach taken:** a `STORED` generated column.

**Alternative not taken:** compute the tsvector in the ingestion task and
write it as an ordinary column. That puts a correctness invariant
("`content_tsv` always matches `content`") in application code, where a
future code path that updates content without updating the vector breaks
search silently. A generated column cannot drift — Postgres enforces it.
The cost is table size and insert-time CPU, both acceptable.

Two details that will bite if missed:

- The **two-argument** `to_tsvector('english', content)` is `IMMUTABLE` and
  therefore legal in a generated column; the one-argument form depends on
  `default_text_search_config` and is only `STABLE`, which Postgres rejects
  here. The config is hardcoded to `'english'` as a consequence —
  per-org language config is not reachable this way, and is out of scope.
- The GIN index is **partial** on `parent_id IS NOT NULL`. Only child chunks
  are searched (parents exist to be sent to the LLM), so indexing parent
  rows would roughly double the index for entries no query can ever match.
  The HNSW index needs no equivalent predicate — parent rows have
  `embedding IS NULL` and pgvector skips them automatically.

On the ORM side the column is mapped with
`Computed("to_tsvector('english', content)", persisted=True)` so SQLAlchemy
excludes it from every `INSERT` (otherwise `bulk_save_objects` in the
ingestion task would try to write it and Postgres would reject the row), and
so Alembic autogenerate doesn't propose dropping it.

### 4c. `organizations.index_version`

```sql
ALTER TABLE organizations
  ADD COLUMN index_version integer NOT NULL DEFAULT 1;
```

Bumped atomically, never read-modify-written in Python:

```sql
UPDATE organizations SET index_version = index_version + 1 WHERE id = :org_id;
```

Bumped when, and only when, the org's searchable corpus changes:

- a document reaches `READY` in `ingest_document` — **in the same commit as
  the status flip**, so no window exists where the chunks are live but the
  cache still serves pre-ingestion answers;
- a document is deleted or re-ingested.

Document deletion has no endpoint today, so that second trigger has nowhere
to live yet — noted in §12 as out of scope, but the bump helper
(`app/services/index_version.py`) should exist so the delete endpoint can
call it rather than reinventing it.

**Alternative not taken:** per-org Redis key prefixes invalidated by
`SCAN`+`DEL`. `SCAN` over a shared Redis to evict one tenant's entries is
O(keyspace) and blocks nothing usefully; a version counter in the key makes
invalidation O(1) and lets stale entries expire on their own TTL.

---

## 5. The real risk: ANN recall under a mandatory tenant filter

This is the part of the phase most likely to produce a quiet,
hard-to-diagnose bug, so it gets its own section.

Every query on `document_chunks` must filter by `org_id` (non-negotiable).
But an HNSW index scan is **not** filtered — pgvector walks the graph for
the nearest `ef_search` candidates across the *whole table*, and the
`WHERE org_id = ?` predicate is applied to that result afterwards. For a
small tenant in a large shared table, the globally-nearest neighbours can be
almost entirely another org's rows, and the query returns far fewer than
`LIMIT 50` — possibly **zero** — while the index reports a perfectly healthy
scan. Nothing errors. Answers just quietly get worse for small tenants as
the table grows, which is the worst possible failure shape.

Note what this is *not*: it is not a tenant leak. The filter is applied, so
no cross-org row is ever returned. It is a recall loss.

**Approach taken:** pgvector's iterative index scans, enabled per
transaction alongside `ef_search`:

```sql
SET LOCAL hnsw.ef_search = 100;
SET LOCAL hnsw.iterative_scan = 'relaxed_order';
SET LOCAL hnsw.max_scan_tuples = 20000;
```

With iterative scan, pgvector resumes the graph walk when the filter
discards too many candidates, until it has enough rows or hits
`max_scan_tuples`. `relaxed_order` over `strict_order` because RRF
re-ranks everything downstream anyway — paying for globally perfect distance
ordering inside the vector branch buys nothing once the ranks are fused with
a second retriever and then reranked.

`SET LOCAL` means these must run in the *same transaction* as the search,
which the retrieval service does explicitly. A plain `SET` would leak the
setting across the pooled connection into unrelated requests.

**Verify on first boot** — iterative scan requires pgvector **≥ 0.8.0** on
the server. The `pgvector/pgvector:pg16` image ships a recent extension, but
I could not confirm the exact version here (the Docker daemon wasn't
running), so migration `0003` should assert it rather than assume:

```sql
SELECT extversion FROM pg_extension WHERE extname = 'vector';
```

If it turns out older, the fallback is to pin a newer image tag; raising
`ef_search` alone does not fix this, it only postpones it.

**Alternative not taken:** partition `document_chunks` by `org_id` (hash or
list), giving each partition its own HNSW index so the tenant filter becomes
partition pruning and the recall problem disappears structurally. This is
the genuinely correct long-term answer and should be revisited when either
tenant count or chunk volume gets real. Not for v1: it's a disruptive
migration of the table Phase 2 just built, hash partitioning makes
per-tenant index maintenance awkward, and list partitioning needs DDL on
every org signup — real operational weight for a problem that iterative scan
handles at current scale.

**Alternative not taken:** a partial HNSW index per org. Perfect recall,
trivially correct — and completely unscalable past a few dozen tenants,
since every signup becomes a `CREATE INDEX`.

---

## 6. Provider interfaces (`app/providers/`)

```
app/providers/
  base.py        EmbeddingProvider, Reranker, LLM protocols + result dataclasses
  openai_impl.py OpenAIEmbeddingProvider, OpenAILLM
  rerank_impl.py the real reranker
  fakes.py       FakeEmbeddingProvider, FakeReranker, FakeLLM
```

**Approach taken:** `typing.Protocol`, structurally typed.

**Alternative not taken:** ABCs with inheritance. Protocols mean the fakes
don't import the real provider module (so tests never risk pulling in a
client that wants an API key at import time), and the real wrappers stay
plain classes. Nothing needs a registry.

```python
class EmbeddingProvider(Protocol):
    model: str
    dimensions: int
    def embed_query(self, text: str) -> list[float]: ...
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

class Reranker(Protocol):
    # Returns (candidate_index, score) sorted best-first, len <= top_n.
    def rerank(self, query: str, candidates: Sequence[str], top_n: int) -> list[RerankResult]: ...

class LLM(Protocol):
    def complete(self, *, system: str, user: str) -> str: ...
    def stream(self, *, system: str, user: str) -> Iterator[str]: ...
```

Note what the `LLM` protocol does *not* have: a `tools` parameter. The "no
tools" decision is enforced by the interface's shape, not by remembering to
pass an empty list at each call site. There is no way to express a tool call
through this type.

Providers are injected via FastAPI dependencies
(`app/api/deps.py`: `get_embedder()`, `get_reranker()`, `get_llm()`) so a
test overrides them the same way `get_db` is already overridden in
`conftest.py`. The embedding provider is shared with ingestion — which
currently constructs `OpenAI(...)` inline in `app/workers/ingestion.py` and
should be refactored onto `OpenAIEmbeddingProvider` so query-time and
index-time embeddings cannot drift apart.

**That drift is a correctness invariant worth stating plainly:** the
embedding model is part of the index. Changing it invalidates every stored
vector, and comparing a new model's query vector against old stored vectors
produces confident nonsense rather than an error. Changing
`EMBEDDING_MODEL` therefore requires re-ingesting every document and
bumping `index_version` for every org — not a config change.

### The fake embedder has to be more than random

A fake returning `[random()] * 1536` makes every retrieval-ordering test
vacuous: it asserts that *something* came back, in an order that means
nothing. `FakeEmbeddingProvider` instead does feature hashing — tokenize,
hash each token into one of 1536 buckets, L2-normalize — so cosine
similarity tracks real token overlap, deterministically. Tests can then
assert that a question about X actually ranks the chunk about X above the
chunk about Y, and that assertion fails if the SQL is wrong.

`FakeLLM` records the exact system and user strings it was handed, which is
what makes the prompt-injection test in §9 possible, and yields a scripted
answer token-by-token so the streaming path is exercised without a network.

---

## 7. Streaming and session lifetime — needs your sign-off

The decision is "never hold a DB session open while streaming an LLM
response." The consequence is sharper than it first looks.

A FastAPI `Depends(get_db)` session is held open until the **response is
fully sent**. When the handler returns a `StreamingResponse`, that includes
the entire generation — so a 20-second answer holds a pooled connection idle
for 20 seconds. Under concurrency the pool is exhausted by sessions doing
nothing. `Depends(get_db)` is therefore unusable on the streaming endpoint,
and that collides head-on with an existing CLAUDE.md code pattern:

> DB session: always via `get_db()` dependency, never created manually

**Proposed amendment**, for you to accept or reject: add a single sanctioned
`session_scope()` context manager to `app/db/session.py`, and narrow the
rule to *"inside a request/response lifecycle, always via `get_db()`;
outside one, always via `session_scope()`, never a bare `SessionLocal()`."*
Code outside a request already exists and already creates sessions by hand —
`app/workers/ingestion.py` opens `SessionLocal()` directly with its own
`try/finally` — so this amendment also brings that file under a rule instead
of leaving it an unacknowledged exception.

The streaming handler then reads:

```python
@router.post("/query/stream")
def query_stream(body: QueryRequest, current_user: User = Depends(get_current_user), ...):
    # No Depends(get_db): a session injected here would stay open for the
    # entire generation. Retrieval opens and closes its own scope, and
    # returns detached plain dataclasses -- never ORM instances, which
    # would raise DetachedInstanceError on first attribute access once
    # the session is gone.
    context = query_service.retrieve(org_id=current_user.org_id, question=body.question, ...)
    return StreamingResponse(query_service.stream_answer(context), media_type="text/event-stream")
```

Two things this forces, both deliberate:

- Everything crossing the session boundary is a frozen dataclass
  (`RetrievedParent`, `Citation`), not a `DocumentChunk`. Returning ORM
  objects from retrieval would work right up until something touched a lazy
  attribute mid-stream and 500'd *inside an already-started response body*,
  where the error cannot be turned into a clean HTTP status.
- Post-stream bookkeeping (cache write, query log) needs a **second,
  fresh** session opened after generation completes. It does not get to
  reuse the retrieval session.

Transport is SSE, and the event order is itself a grounding mechanism:

```
event: citations   {"citations": [{"index":1,"document_id":"…","filename":"…","page":4}, …]}
event: token       {"text": "Per the "}
event: token       {"text": "Q3 policy "}
event: done        {"cached": false}
```

Citations go out **before** the first token because they are the retrieved
set — they come from the database, not from the model. The model is asked to
reference them as `[1]`, `[2]`, and any index it emits outside the range it
was given is stripped during validation (§9). A model cannot invent a source
through this channel; the worst it can do is miscite one that exists.

Citation display metadata (`filename`) comes from a join to `documents`
across the ≤5 retrieved parents. That's a deliberate contrast with the
`document_chunks.org_id` denormalization: the join is on the cold path over
a handful of rows, where the hot-path argument for denormalizing simply
doesn't apply.

---

## 8. Caching

Redis, key exactly as decided:

```
documind:answer:{sha256(org_id + "\x00" + normalized_rewritten_question + "\x00" + index_version)}
```

Value: JSON `{answer, citations, model, created_at}`. TTL 24h as a backstop;
`index_version` is what actually keeps it correct.

Normalization (applied to the *rewritten* question): strip, collapse
internal whitespace, casefold. Nothing cleverer — stemming or punctuation
stripping at this layer would silently merge questions that aren't the same
question.

A cache hit is replayed through the identical SSE event sequence so clients
have one code path, with `{"cached": true}` on the `done` event.

**One consequence of the decided key worth stating:** because the key uses
the *rewritten* question, the rewrite LLM call happens before the lookup and
is paid on every single request, cache hit included. The decision buys a
higher hit rate — "what's the PTO policy?" and "how much vacation do I get?"
can collapse to one entry — at the cost of never having a fully
LLM-free request.

**Additive mitigation, not a change to the decision:** a second cache keyed
on the *raw* normalized question →

```
documind:rewrite:{sha256(org_id + "\x00" + normalized_raw_question)}  →  rewritten question
```

A repeated identical question then hits the rewrite cache, reconstructs the
same answer key, and hits the answer cache — zero LLM calls. The decided
answer-cache key is untouched. This one needs no `index_version`: a rewrite
depends on the question, not on the corpus.

Only successful answers are cached. Refusals from the grounding gate (§3
step 7) are not — they're cheap to recompute, and caching a refusal keyed on
an `index_version` that a just-finished ingestion is about to bump is a race
with no upside.

---

## 9. Prompt injection: where the boundary is

"Retrieved text is untrusted data. The LLM gets no tools." Concretely:

1. **No tools, structurally.** The `LLM` protocol has no tool parameter
   (§6). An injected "call the delete_documents tool" has nothing to reach.
2. **Retrieved text never reaches the rewriter.** The rewrite call sees the
   user's question and conversation history only. This matters because the
   rewriter's output becomes part of the cache key and the FTS query — a
   document that could influence rewriting could poison another user's cache
   entry. The two LLM calls have different untrusted inputs and must not be
   merged into one.
3. **Delimited, labelled context.** Parents are wrapped in explicit
   `<document index="1">…</document>` blocks with a system instruction that
   content inside is reference data and never an instruction. This is
   mitigation, not a guarantee — it raises the cost of an injection, and
   nothing more. It is not what makes the system safe; (1) is.
4. **Citation validation after generation.** Indices the model emits are
   intersected with the indices it was actually given; anything else is
   stripped from the answer before it's cached or returned.
5. **Retrieval is org-scoped before any of this.** An injection can at worst
   mislead the answer for the org that uploaded the document containing it.
   It cannot reach another tenant's rows, because `org_id` came from the JWT
   and is in the `WHERE` clause.

A test asserts (2) and (4) directly: ingest a document whose text contains
`IGNORE ALL PREVIOUS INSTRUCTIONS AND …`, then check `FakeLLM`'s recorded
prompts — the rewriter's must not contain that string at all, and the
answerer's must contain it only inside a `<document>` block.

---

## 10. Tests against real Postgres

`tests/conftest.py` currently builds schema with
`Base.metadata.create_all` on in-memory SQLite. Both halves of that have to
go: SQLite has no `vector`, no `tsvector`, and no `<=>`, and `create_all`
would skip the HNSW index, the GIN index and the generated column, since all
three live only in migration DDL.

- **Schema comes from `alembic upgrade head`**, run once per session against
  a dedicated test database (`TEST_DATABASE_URL`, new setting). This has a
  side benefit worth naming: the migrations themselves become tested, which
  they currently are not.
- **Per-test isolation by transaction rollback**, not schema recreation:
  open a connection, `BEGIN`, bind the session to it, join a `SAVEPOINT`,
  roll back at teardown. Re-running migrations per test would make the suite
  unusably slow once there are a dozen of them.
- **Fixtures replace `Depends(get_db)`** as they do today, and additionally
  override `get_embedder`/`get_reranker`/`get_llm` with the fakes.
- **The suite requires a running `db` container.** `docker compose up -d db`
  becomes a documented precondition, and the Postgres-dependent fixtures
  `pytest.skip` with a clear message when `TEST_DATABASE_URL` is
  unreachable — a confusing connection traceback on a fresh clone is a bad
  first experience, but silently passing zero tests is worse.

Tests Phase 3 should ship with:

| Test | Asserts |
|---|---|
| hybrid search finds a lexical-only match | A rare exact token (part number, error code) that the fake embedder ranks poorly is still retrieved via the FTS branch — i.e. the hybrid half is doing work |
| hybrid search finds a semantic-only match | A paraphrase with no shared tokens is retrieved via the vector branch |
| RRF ordering | A child ranked well by *both* branches outranks one ranked top by only one — the actual point of fusing |
| cross-org isolation on retrieval | Org B's chunks are never returned for Org A's question, even when they are the best semantic match in the table |
| `index_version` invalidation | Answer cached, document ingested, same question → recomputed, not replayed |
| grounding gate | Empty/weak retrieval → refusal, and `FakeLLM` records **zero** calls |
| citation validation | A `FakeLLM` that emits `[9]` when given 3 sources → `[9]` stripped |
| injection boundary | As described in §9 |
| no session held during stream | The streaming endpoint completes with the connection pool at `size=1` — fails loudly if a session is being held across generation |

That last one is the only test here that directly guards a decision which is
otherwise invisible to assertions, which is exactly why it's worth writing.

---

## 11. Endpoints, schemas, files

```
POST /api/v1/query          → QueryResponse  (JSON; answer, citations, cached)
POST /api/v1/query/stream   → text/event-stream (§7)
```

Both `Depends(get_current_user)` — members ask questions; `require_admin` is
for corpus mutation, not reading. `org_id` comes off the JWT and is never
accepted in the body, which the `QueryRequest` schema enforces by simply not
having the field.

New files:

```
app/providers/{base,openai_impl,rerank_impl,fakes}.py
app/services/retrieval.py        hybrid search + RRF; the only module with raw SQL
app/services/query_service.py    orchestration: rewrite → cache → retrieve → gate → stream
app/services/rewriter.py         question rewriting + normalization
app/services/cache.py            Redis get/set, key construction
app/services/index_version.py    atomic bump helper
app/schemas/query.py             QueryRequest, QueryResponse, Citation
app/api/v1/routes/query.py       both endpoints, thin
alembic/versions/0003_*.py       HNSW swap, tsvector + GIN, index_version
```

Changed files: `app/models/document_chunk.py` (`content_tsv`),
`app/models/organization.py` (`index_version`), `app/api/deps.py` (provider
deps), `app/db/session.py` (`session_scope`), `app/workers/ingestion.py`
(bump `index_version`; use `OpenAIEmbeddingProvider`), `app/main.py`
(register router), `app/core/config.py` (new settings),
`tests/conftest.py` (rewritten per §10).

Keeping all raw SQL in `app/services/retrieval.py` is deliberate: the RRF
query is the one place in the codebase where the `org_id` filter is written
by hand rather than through the ORM, so it should be the one file a reviewer
has to read carefully for tenant-filter correctness.

### The RRF query

```sql
WITH vec AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> :qvec, id) AS rank
    FROM document_chunks
    WHERE org_id = :org_id AND parent_id IS NOT NULL AND embedding IS NOT NULL
    ORDER BY embedding <=> :qvec, id
    LIMIT :branch_k
),
fts AS (
    SELECT c.id,
           ROW_NUMBER() OVER (ORDER BY ts_rank_cd(c.content_tsv, q.query) DESC, c.id) AS rank
    FROM document_chunks c, websearch_to_tsquery('english', :q_text) AS q(query)
    WHERE c.org_id = :org_id AND c.parent_id IS NOT NULL AND c.content_tsv @@ q.query
    ORDER BY ts_rank_cd(c.content_tsv, q.query) DESC, c.id
    LIMIT :branch_k
)
SELECT COALESCE(v.id, f.id) AS chunk_id,
       COALESCE(1.0 / (:rrf_k + v.rank), 0) + COALESCE(1.0 / (:rrf_k + f.rank), 0) AS score
FROM vec v FULL OUTER JOIN fts f ON v.id = f.id
ORDER BY score DESC, chunk_id
LIMIT :fused_n;
```

Notes on choices embedded in that query:

- **Fused in SQL, one round trip.** The alternative — two queries merged in
  Python — would ship 100 rows of chunk text over the wire to compute two
  integer ranks, then throw most of it away. Only ids and scores cross the
  boundary here; text is fetched once, for the survivors.
- **`websearch_to_tsquery`, not `to_tsquery`.** The input is a user
  question. `to_tsquery` raises a syntax error on unescaped punctuation —
  a question containing `&` would 500. `websearch_to_tsquery` never
  errors on arbitrary input and gives quoted-phrase and `-exclusion`
  support for free.
- **`ts_rank_cd`, not `ts_rank`.** Cover density rewards terms appearing
  near each other, which is the right signal for 125-token chunks.
- **`id` as a tiebreaker in every `ORDER BY`.** Without it, equal scores
  order arbitrarily, and ordering assertions in §10 become flaky in a way
  that will cost an afternoon to diagnose.
- **`k = 60` for RRF**, the value from the original Cormack et al. RRF
  paper, as a setting. It controls how steeply rank-1 outweighs rank-10.
- **`FULL OUTER JOIN`** so a chunk found by only one branch still scores,
  with the missing branch contributing 0 rather than dropping the row.

---

## 12. Out of scope for Phase 3

- Multi-turn conversations. The rewriter accepts optional history, but
  nothing persists it — no `conversations`/`messages` tables. Phase 4.
- Document delete / re-ingest endpoints. `index_version.bump()` will exist
  and be unused by any delete path, because no delete path exists.
- Per-org language configuration for FTS (hardcoded `'english'`, §4b).
- Table partitioning by `org_id` (§5) — the planned escape hatch, revisited
  when volume justifies it.
- Answer-quality evaluation (a golden Q/A set, retrieval metrics). The §10
  tests prove the pipeline is *correct*; they say nothing about whether the
  answers are *good*. That's its own phase and it needs real documents.

---

## 13. What needs your decision before implementation starts

1. **§2** — add `POST /auth/login` as part of Phase 3, or defer again?
2. **§7** — accept the CLAUDE.md amendment narrowing the `get_db()` rule
   and adding `session_scope()`? The streaming decision can't be
   implemented without resolving this one.
3. **§8** — take the additive rewrite cache, or accept one LLM call on every
   request?
4. **§4a** — confirm HNSW `m`/`ef_construction` stay at defaults for v1.
