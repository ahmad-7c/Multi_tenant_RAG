import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.api.v1.routes import auth, conversations, documents, usage, users
from app.core.config import settings
from app.core.http import aclose_async_client
from app.core.startup_checks import enforce_production_safety, is_production
from app.db.session import SessionLocal, get_db
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
    # BEFORE anything else, and it RAISES rather than degrading.
    #
    # Every other guard here keeps the process alive on purpose. This
    # one must not: a process signing tokens with the documented
    # placeholder secret lets anyone who has read the repository mint
    # an admin token for any organization. Raising inside the lifespan
    # means uvicorn exits non-zero, so Docker's restart policy surfaces
    # it as a crash-loop an operator can see -- rather than a service
    # that looks healthy and is not.
    enforce_production_safety(settings)

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
        # /health/ready is what catches it. Not a reason to refuse queries
        # that might work once it comes back.
        #
        # This comment used to claim "the readiness probe will catch it"
        # while no readiness probe existed anywhere in the tree, so the
        # condition it waves through had NO probe at all: with Postgres
        # down the process logged this one warning, reported startup
        # complete, served /health as "ok" and 500ed every authenticated
        # route. Measured on a dev box, then fixed by writing the probe
        # this line already assumed.
        logger.warning("embedding-model guard could not run", exc_info=True)
    finally:
        db.close()

    yield

    # Shutdown. Closes the pooled AsyncClient for this loop so in-flight
    # keepalive connections are released deliberately rather than at
    # interpreter exit, where httpx can only warn about them.
    await aclose_async_client()


def configure_cors(target: FastAPI) -> list[str]:
    """Add CORS to `target`, but only if an operator asked for it.

    A function taking the app rather than inline module code, so a test
    can exercise the real wiring on a throwaway FastAPI instance. The
    alternative -- reloading this module to pick up a new setting --
    replaces the module-level `app` with a new object while the rest of
    the suite still holds the old one, which turns one CORS test into
    unrelated failures elsewhere. (Observed, then fixed this way.)

    Returns the origins configured, for logging and for tests.

    CORS is OPT-IN and off by default. The deployed architecture is a
    same-origin Next.js BFF: the browser talks to Next, Next talks to
    this API server-side, so there is no cross-origin request to permit
    and no header should be advertised. An empty setting therefore adds
    NO middleware at all rather than adding a permissive default.

    Setting CORS_ALLOW_ORIGINS turns it on for direct browser access --
    a test client, Swagger on another port, a separate dev frontend.
    """
    origins = settings.cors_allow_origins()
    if not origins:
        return []

    target.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        # Cookies and Authorization headers. The whole reason to enable
        # this is authenticated access; without it a caller can only
        # reach /health.
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        # A cross-origin client cannot READ a response header unless it
        # is exposed. Retry-After is the one carrying meaning the UI
        # acts on -- without this a 429 countdown silently reads as
        # undefined while the header sits on the wire.
        expose_headers=["Retry-After"],
    )
    logger.warning(
        "CORS is ENABLED for %d origin(s): %s. This is not needed for the "
        "same-origin BFF deployment; unset CORS_ALLOW_ORIGINS in production "
        "unless a browser calls this API directly.",
        len(origins),
        ", ".join(origins),
    )
    return origins


# The interactive docs publish the whole API shape -- every route,
# every schema, including the citations-event contract. Useful in
# development, and reconnaissance in production, so they are off there.
# None disables the route entirely rather than hiding a link to it.
_docs_enabled = not is_production(settings)

app = FastAPI(
    title="DocuMind",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

configure_cors(app)

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


@app.get("/health/ready")
def ready(db: Session = Depends(get_db)):
    """Readiness: would a real request actually succeed right now?

    A SECOND endpoint rather than a field on /health, because the two
    answer different questions and one status word cannot answer both.

    /health is liveness plus quality degradations and must stay "ok"
    through all of them -- a load balancer reads it to decide whether
    to keep this instance in rotation, and pulling a working instance
    because the reranker breaker tripped turns a degradation into an
    outage. That reasoning is unchanged and /health is untouched.

    This endpoint is the opposite, and it exists because the gap was
    measured rather than imagined: with Postgres unreachable, /health
    returned 200 {"status": "ok"} while POST /api/v1/auth/login
    returned 500, because every authenticated route begins with a
    query and the lifespan deliberately logs a warning and continues
    (the database may come back). Nothing anywhere reported the one
    condition that makes the whole API useless.

    The container HEALTHCHECK points HERE, not at /health. That is
    what makes `depends_on: service_healthy` and the deploy's health
    gate honest: an api that cannot reach its database, or whose
    credentials or migrations are wrong, must fail the check so the
    automatic rollback fires. Pointed at /health it would report
    success and the rollback would never run.

    SELECT 1, not a count or a table read: the question is whether a
    connection can be checked out of the pool and a statement
    round-tripped, which is precisely what every route needs and
    nothing more. `pool_pre_ping=True` on the engine means a stale
    pooled connection is replaced rather than reported as a failure.

    Redis is deliberately NOT checked. The app is built to survive
    losing it -- auth and reads fail open, the answer path falls back
    to a per-process bucket -- and /health already names that as
    `rate_limiter_degraded`. Failing readiness on Redis would refuse
    traffic this process can still serve, which is the mistake
    /health's own docstring warns against.
    """
    try:
        db.execute(sql_text("SELECT 1"))
    except Exception:
        # The body names the failing dependency and NOTHING else. A
        # driver's exception text carries the host, port and user from
        # the DSN, and this endpoint is reachable through the edge --
        # so the detail goes to the operator's log and the response
        # stays a bare word. Same rule as the user-facing failure
        # codes: never a backend URL in a response.
        logger.exception("readiness probe failed: database unreachable")
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "failed": ["database"]},
        )

    return {"status": "ready"}
