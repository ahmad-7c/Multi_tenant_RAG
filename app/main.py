import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.routes import auth, conversations, documents, users
from app.core.config import settings
from app.core.http import aclose_async_client
from app.db.session import SessionLocal
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


@app.get("/health")
def health():
    return {"status": "ok"}
