from typing import Literal, Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every tunable the system has lives here and is env-overridable.

    Nothing downstream is allowed to hardcode a model name, a token budget,
    or a threshold: a value that only exists in code can't be changed per
    environment without a redeploy, and the retrieval knobs in particular
    get re-tuned against real corpora rather than guessed once.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- Infrastructure -------------------------------------------------
    DATABASE_URL: str
    # Separate DB so the test suite can DROP/CREATE schema freely without
    # ever touching dev data. Tests refuse to run if this is unset.
    TEST_DATABASE_URL: Optional[str] = None
    REDIS_URL: str = "redis://localhost:6379/0"
    ENV: str = "development"

    # ---- Celery ---------------------------------------------------------
    # Default to REDIS_URL, but overridable so broker, result backend and
    # the answer cache can be split onto separate Redis databases -- a
    # FLUSHDB aimed at the cache must not be able to drop queued tasks.
    CELERY_BROKER_URL: Optional[str] = None
    CELERY_RESULT_BACKEND: Optional[str] = None
    CELERY_TASK_SOFT_TIME_LIMIT: int = 900
    CELERY_TASK_TIME_LIMIT: int = 960
    # MUST exceed CELERY_TASK_TIME_LIMIT; see the comment in celery_app.py.
    CELERY_VISIBILITY_TIMEOUT: int = 1800
    CELERY_RESULT_EXPIRES: int = 3600
    CELERY_MAX_TASKS_PER_CHILD: int = 100

    # ---- Auth -----------------------------------------------------------
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60 * 24

    # ---- Storage --------------------------------------------------------
    # "local" writes to STORAGE_LOCAL_ROOT (dev); "s3" talks to any
    # S3-compatible endpoint (AWS S3, Supabase Storage, Cloudflare R2).
    STORAGE_BACKEND: Literal["local", "s3"] = "local"
    STORAGE_LOCAL_ROOT: str = "uploads"
    S3_BUCKET: Optional[str] = None
    # Set for non-AWS S3-compatible providers; left None for real AWS S3.
    S3_ENDPOINT_URL: Optional[str] = None
    S3_REGION: str = "us-east-1"
    S3_ACCESS_KEY_ID: Optional[str] = None
    S3_SECRET_ACCESS_KEY: Optional[str] = None

    # ---- Uploads --------------------------------------------------------
    MAX_UPLOAD_BYTES: int = 25 * 1024 * 1024

    # ---- Providers (never hardcode a model name anywhere else) ----------
    OPENAI_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None
    # One base URL for every Gemini call (embeddings and generation). Kept
    # configurable so a regional endpoint or a local proxy needs no code
    # change -- and so tests can point it at something unroutable.
    GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta"

    EMBEDDING_PROVIDER: Literal["gemini", "openai", "fake"] = "gemini"
    # gemini-embedding-001, not -002/-2: the newer model is multimodal and
    # self-normalizing but DROPPED the taskType parameter, and asymmetric
    # document/query embedding matters more to a text-only RAG system than
    # multimodality it never uses.
    EMBEDDING_MODEL: str = "gemini-embedding-001"
    # Must match the Vector(...) dimension in the migration. Changing the
    # model to one with a different width is a migration, not a config edit.
    # Gemini accepts 128..3072 and recommends 768/1536/3072; 1536 scores the
    # same on MTEB as 3072, so the existing column width costs nothing.
    EMBEDDING_DIMENSIONS: int = 1536

    ANTHROPIC_API_KEY: Optional[str] = None
    LLM_PROVIDER: Literal["gemini", "anthropic", "fake"] = "gemini"
    # The answering model: quality matters, it sees the retrieved context.
    # flash-lite rather than flash because the free tier's request quota
    # is the binding constraint here, and grounded extraction over
    # already-reranked context is not the hardest thing a model does.
    LLM_MODEL: str = "gemini-3.5-flash-lite"
    LLM_MAX_TOKENS: int = 1024
    # PER-OPERATION stall limit, handed to httpx. Bounds how long ONE
    # read may wait with no bytes arriving. It does NOT bound the call:
    # a model emitting one token every 25s forever never trips it.
    LLM_TIMEOUT_SECONDS: float = 30.0
    # TOTAL deadline for the whole generation, enforced with
    # asyncio.timeout around the entire stream. The other half of the
    # guarantee: without it a slow-but-never-stalled stream runs forever
    # and holds a request handler open with it.
    LLM_TOTAL_TIMEOUT_SECONDS: float = 120.0
    # Retries apply ONLY before the first token reaches the client --
    # after that the bytes are already on the wire and a second attempt
    # would splice two different answers together. Kept at 1: this runs
    # inside the user's request, where a long retry chain is
    # indistinguishable from a hang.
    LLM_MAX_RETRIES: int = 1
    LLM_RETRY_BASE_SECONDS: float = 1.0
    # A 429 often carries Retry-After, which is the provider stating
    # exactly how long to wait -- better information than our guess, so
    # it wins when it is short. A LONG Retry-After is a different
    # message ("not soon"); honouring 90s inside a request handler is a
    # hang with extra steps, so past this we surface the rate-limit
    # sentence immediately and free the handler.
    LLM_RETRY_AFTER_MAX_SECONDS: float = 5.0
    # Low, not zero: grounded extraction wants determinism, but exactly 0
    # makes repeated near-identical questions produce brittle phrasing.
    LLM_TEMPERATURE: float = 0.1

    # The query rewriter only reshapes a follow-up into a standalone
    # question -- a small/cheap model is enough, and it runs on every
    # request including cache hits, so it must be cheap.
    REWRITE_MODEL: str = "gemini-3.5-flash-lite"
    REWRITE_MAX_TOKENS: int = 128
    REWRITE_HISTORY_TURNS: int = 4

    RERANKER_PROVIDER: Literal["hosted", "passthrough", "fake"] = "passthrough"
    RERANKER_MODEL: str = "rerank-v3.5"
    # Reads COHERE_API_KEY too. The canonical name stays provider-neutral
    # because RERANKER_BASE_URL is configurable (Jina, Voyage and Together
    # all accept this request shape), but "COHERE_API_KEY" is what a
    # person writes down when Cohere is the service that issued the key --
    # and the cost of not accepting it is silent: no key means the hosted
    # reranker is never constructed, so reranking is simply off, with
    # nothing raised and nothing logged.
    RERANKER_API_KEY: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("RERANKER_API_KEY", "COHERE_API_KEY"),
    )
    RERANKER_BASE_URL: str = "https://api.cohere.com/v2/rerank"
    RERANKER_TIMEOUT_SECONDS: float = 3.0
    # Sent explicitly rather than relying on Cohere's default of the same
    # value: per-document truncation decides what the cross-encoder
    # actually scored, so it must not be able to change under us when the
    # provider changes a default. Child chunks are CHILD_TOKENS (~125), so
    # this truncates nothing today -- it is here to keep it that way.
    RERANKER_MAX_TOKENS_PER_DOC: int = 4096
    # Cohere rejects a request carrying more than 1000 documents. We send
    # RETRIEVE_CANDIDATES (~50), so this is a guard against a future
    # config bump turning every query into a 400, not a live constraint.
    RERANKER_MAX_DOCUMENTS: int = 1000

    # ---- HTTP connection pool -------------------------------------------
    # Shared across every provider call. Small on purpose: the pool caps
    # in-flight provider requests, and on a free tier the per-minute
    # request quota binds long before the socket count does.
    HTTP_MAX_CONNECTIONS: int = 20
    HTTP_MAX_KEEPALIVE_CONNECTIONS: int = 10
    # Below the idle timeout of a typical provider load balancer (60s+).
    # A pooled connection the far end has already dropped is worse than
    # none: the next request spends its budget finding out the socket is
    # dead. Expiring ours first keeps that off the user's query.
    HTTP_KEEPALIVE_EXPIRY_SECONDS: float = 30.0
    # Connect is split from read because they mean different things: a
    # hanging connect is a dead endpoint and should fail fast, a slow
    # read is a model thinking.
    HTTP_CONNECT_TIMEOUT_SECONDS: float = 5.0
    # Backstop only -- every provider passes its own per-request timeout.
    # Never None, which in httpx means "wait forever".
    HTTP_DEFAULT_TIMEOUT_SECONDS: float = 30.0

    # ---- Extraction -----------------------------------------------------
    # A scanned PDF extracts to almost nothing. Below this mean
    # chars-per-page we refuse the document with a "needs OCR" error rather
    # than storing a handful of junk chunks that retrieval can never match.
    # Deliberately low: a sparse-but-real PDF (title pages, figures) should
    # pass, and only a genuinely image-only document should fail.
    OCR_MIN_CHARS_PER_PAGE: int = 25

    # ---- Cleaning -------------------------------------------------------
    # How many lines at the top/bottom of each page to consider as a
    # candidate running header/footer.
    CLEAN_EDGE_LINES: int = 3
    # Repeated-line removal needs enough pages to tell a running header
    # from a line that simply happens to appear twice.
    CLEAN_MIN_PAGES_FOR_HEADER_DETECTION: int = 3
    # Fraction of pages a candidate line must appear on to count as a
    # running header/footer.
    CLEAN_REPEAT_RATIO: float = 0.6
    # Running headers/footers are short. Without a cap, digit
    # normalization ("Page 3 of 10" -> "Page # of #") makes any prose
    # sentence containing a number collide with the same sentence on
    # another page, and real content gets deleted.
    CLEAN_MAX_HEADER_CHARS: int = 80

    # ---- Chunking -------------------------------------------------------
    PARENT_TOKENS: int = 500
    CHILD_TOKENS: int = 125
    CHILD_OVERLAP: int = 20

    # ---- Embedding ------------------------------------------------------
    EMBED_BATCH_SIZE: int = 64
    EMBED_MAX_RETRIES: int = 3
    # Backoff base: sleep is EMBED_RETRY_BASE_SECONDS * 2**attempt.
    EMBED_RETRY_BASE_SECONDS: float = 5.0
    EMBED_TIMEOUT_SECONDS: float = 30.0
    # Client-side floor on request spacing, in requests per minute; 0
    # disables it. Set below the free tier's advertised limit on purpose:
    # the published number is a ceiling the provider enforces, and being
    # at it means every jitter spike becomes a 429. This throttles HTTP
    # REQUESTS, not texts -- EMBED_BATCH_SIZE texts ride in one request.
    EMBED_REQUESTS_PER_MINUTE: int = 90

    # ---- Retrieval ------------------------------------------------------
    # Candidates surviving the RRF merge of vector + full-text results.
    RETRIEVE_CANDIDATES: int = 50
    # Rows each leg fetches before the merge. Larger than
    # RETRIEVE_CANDIDATES on purpose: RRF can only rank what it is given,
    # so a document that one leg ranks 55th and the other ranks 2nd must
    # still be visible to the merge.
    RETRIEVE_PER_LEG: int = 60
    # RRF's rank-smoothing constant. 60 is the value from the original
    # Cormack et al. paper and the de-facto default; it controls how
    # steeply rank 1 outweighs rank 10.
    RRF_K: int = 60
    # HNSW search breadth. Higher = better recall, more time.
    HNSW_EF_SEARCH: int = 100
    # pgvector >= 0.8 iterative scans: resume the graph walk when the
    # tenant filter discards too many candidates. Without this a small
    # tenant in a large table silently gets a short result set.
    # "relaxed_order" because RRF re-ranks everything downstream anyway.
    HNSW_ITERATIVE_SCAN: str = "relaxed_order"
    HNSW_MAX_SCAN_TUPLES: int = 20000
    # Postgres text-search configuration. Must match migration 0004's
    # generated column, which is baked to 'english'.
    TS_CONFIG: str = "english"
    RERANK_TOP_N: int = 8
    # Reranker-specific and uncalibrated until Phase 4; see the gate's
    # comment in app/services/reranker.py.
    CONFIDENCE_THRESHOLD: float = 0.3
    CONTEXT_TOKEN_BUDGET: int = 6000

    # ---- Cache ----------------------------------------------------------
    CACHE_TTL_SECONDS: int = 3600
    # Defaults to REDIS_URL. Split onto its own Redis database when the
    # broker shares the instance: a FLUSHDB aimed at the cache must not be
    # able to drop queued ingestion tasks.
    CACHE_REDIS_URL: Optional[str] = None


settings = Settings()
