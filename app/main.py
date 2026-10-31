import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.routes import auth, conversations, documents, usage, users
from app.core.config import settings
from app.core.http import aclose_async_client
from app.db.session import SessionLocal
from app.services.reranker import check_reranker_config, get_reranker
from app.services.embedding import (
    EmbeddingModelMismatch,
    check_embedding_model_consistency,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup checks, and shutdown of the shared connection pool.

    The embedding-model guard runs ONCE here rather than per request.
    That is sufficient, not a shortcut: the only way stored vectors can
    come from a model other than the configured one is a config change,
    and a config change requires a restart -- which runs this again.
    Putting it on the query path would add a DISTINCT scan of
    document_chunks to every question to detect a condition that cannot
    arise while the process is up.

    The process does NOT exit on a mismatch. A hard exit takes down
    uploads, auth and document management too, none of which depend on
    vector comparability, and it turns a recoverable misconfiguration
    into an outage plus a crash-loop. Instead the condition is recorded
    and the query endpoints alone refuse.
    """
    app.state.embedding_model_error = None

    # Cheap, no I/O. Catches the misconfiguration that costs answer
    # quality without raising anything: a reranker key set while the
    # provider is still passthrough.
    check_reranker_config()
    db = SessionLocal()
    try:
        check_embedding_model_consistency(db, configured_model=settings.EMBEDDING_MODEL)
    except EmbeddingModelMismatch as exc:
        app.state.embedding_model_error = str(exc)
        # critical, not warning: answers are silently wrong until this is
        # resolved, and nothing else in the system will mention it again.
        logger.critical("REFUSING TO SERVE QUERIES: %s", exc)
    except Exception:
        # A DB that is not reachable at startup is a separate problem and
        # the readiness probe will catch it. Not a reason to refuse
        # queries that might work once it comes back.
        logger.warning("embedding-model guard could not run", exc_info=True)
    finally:
        db.close()

    yield

    # Shutdown. Closes the pooled AsyncClient for this loop so in-flight
    # keepalive connections are released deliberately rather than at
    # interpreter exit, where httpx can only warn about them.
    await aclose_async_client()


app = FastAPI(title="DocuMind", version="0.1.0", lifespan=lifespan)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(documents.router, prefix="/api/v1")
app.include_router(conversations.router, prefix="/api/v1")
app.include_router(usage.router, prefix="/api/v1")


@app.get("/health")
def health():
    """Liveness, plus the degradations that are otherwise invisible.

    `status` stays "ok" for every degradation listed here, deliberately.
    A load balancer reads this endpoint to decide whether to send
    traffic, and none of these conditions mean this instance cannot
    serve: a disabled reranker still answers (on RRF order), and the
    embedding-model mismatch already refuses only the query routes. A
    non-ok status would pull a working instance out of rotation over a
    quality problem and turn a degradation into an outage.

    `degraded` exists because the alternative was nothing. The reranker
    breaker logged one ERROR when it tripped and then went quiet by
    design -- so an operator who missed that single line had no way to
    ask whether reranking was still running.
    """
    degraded: list[str] = []

    if app.state.embedding_model_error:
        degraded.append("embedding_model_mismatch")

    # getattr: passthrough and fake rerankers have no breaker at all.
    if getattr(get_reranker(), "is_disabled", False):
        degraded.append("reranker_disabled")

    # The rate limiter running on per-process buckets is a degradation
    # worth naming: the configured limit is no longer the effective one
    # (N processes means N buckets) and the daily budget guard cannot be
    # evaluated at all. Without this line the only evidence is a WARNING
    # that was logged once, which is exactly the gap `degraded` exists
    # to close for the reranker breaker.
    from app.api.limits import get_limiter as _get_limiter

    if settings.RATE_LIMIT_ENABLED and getattr(
        _get_limiter(), "fallback_activations", 0
    ):
        degraded.append("rate_limiter_degraded")

    body: dict = {"status": "ok"}
    if degraded:
        body["degraded"] = degraded
    return body
