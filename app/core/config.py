from typing import Literal, Optional

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
    EMBEDDING_PROVIDER: Literal["openai", "fake"] = "openai"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    # Must match the Vector(...) dimension in the migration. Changing the
    # model to one with a different width is a migration, not a config edit.
    EMBEDDING_DIMENSIONS: int = 1536

    ANTHROPIC_API_KEY: Optional[str] = None
    LLM_PROVIDER: Literal["anthropic", "fake"] = "anthropic"
    # The answering model: quality matters, it sees the retrieved context.
    LLM_MODEL: str = "claude-sonnet-5"
    LLM_MAX_TOKENS: int = 1024
    # Low, not zero: grounded extraction wants determinism, but exactly 0
    # makes repeated near-identical questions produce brittle phrasing.
    LLM_TEMPERATURE: float = 0.1

    # The query rewriter only reshapes a follow-up into a standalone
    # question -- a small/cheap model is enough, and it runs on every
    # request including cache hits, so it must be cheap.
    REWRITE_MODEL: str = "claude-haiku-4-5-20251001"
    REWRITE_MAX_TOKENS: int = 128
    REWRITE_HISTORY_TURNS: int = 4

    RERANKER_PROVIDER: Literal["hosted", "passthrough", "fake"] = "passthrough"
    RERANKER_MODEL: str = "rerank-v3.5"
    RERANKER_API_KEY: Optional[str] = None
    RERANKER_BASE_URL: str = "https://api.cohere.com/v2/rerank"
    RERANKER_TIMEOUT_SECONDS: float = 3.0

    # ---- Chunking -------------------------------------------------------
    PARENT_TOKENS: int = 500
    CHILD_TOKENS: int = 125
    CHILD_OVERLAP: int = 20

    # ---- Embedding ------------------------------------------------------
    EMBED_BATCH_SIZE: int = 64
    EMBED_MAX_RETRIES: int = 3

    # ---- Retrieval ------------------------------------------------------
    # Candidates surviving the RRF merge of vector + full-text results.
    RETRIEVE_CANDIDATES: int = 50
    RERANK_TOP_N: int = 8
    # Reranker-specific and uncalibrated until Phase 4; see the gate's
    # comment in app/services/reranker.py.
    CONFIDENCE_THRESHOLD: float = 0.3
    CONTEXT_TOKEN_BUDGET: int = 6000

    # ---- Cache ----------------------------------------------------------
    CACHE_TTL_SECONDS: int = 3600


settings = Settings()
