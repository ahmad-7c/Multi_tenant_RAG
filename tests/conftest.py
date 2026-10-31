"""Test harness.

These tests run against a real Postgres + pgvector database, not SQLite.
That is a deliberate cost: SQLite cannot express the things this system
actually depends on -- the `vector` type and its operators, HNSW/GIN
indexes, `to_tsvector`/`ts_rank`, generated columns, or native enums. A
suite that passes on SQLite tells you nothing about whether retrieval
works, which is the only interesting question here.

Schema comes from Alembic, not Base.metadata.create_all(), so the suite
also proves the migrations are runnable and complete.
"""
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401 -- side effect: registers models on Base.metadata
from app.core.config import settings
from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.main import app as fastapi_app
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.services import document_service
from app.services.storage import get_storage

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True, scope="session")
def _no_live_providers():
    """The suite is offline BY CONSTRUCTION, not by what .env happens to say.

    Settings are read from .env, and .env is where real keys and real
    provider names live -- so the moment someone flips
    EMBEDDING_PROVIDER=gemini to do a live run, every test that reaches
    a provider factory starts making paid network calls. Found exactly
    that way: the live smoke test required flipping .env, and nothing in
    the harness would have stopped the next `pytest` from billing for
    it.

    Tests that want a real provider construct one explicitly with an
    injected client, which this does not affect. Live verification lives
    in scripts/smoke_live.py and is run by hand.
    """
    from app.services import embedding, llm, reranker

    originals = (
        settings.EMBEDDING_PROVIDER,
        settings.LLM_PROVIDER,
        settings.RERANKER_PROVIDER,
    )
    settings.EMBEDDING_PROVIDER = "fake"
    settings.LLM_PROVIDER = "fake"
    settings.RERANKER_PROVIDER = "fake"
    # The factories are lru_cached, so a provider built before this ran
    # would survive the override.
    for factory in (
        embedding.get_embedding_provider,
        llm.get_llm,
        llm.get_rewrite_llm,
        llm.get_judge_llm,
        reranker.get_reranker,
    ):
        factory.cache_clear()

    yield

    (
        settings.EMBEDDING_PROVIDER,
        settings.LLM_PROVIDER,
        settings.RERANKER_PROVIDER,
    ) = originals
    for factory in (
        embedding.get_embedding_provider,
        llm.get_llm,
        llm.get_rewrite_llm,
        llm.get_judge_llm,
        reranker.get_reranker,
    ):
        factory.cache_clear()


@pytest.fixture(autouse=True)
def fresh_rate_limiter():
    """A clean per-process limiter for every test.

    Two separate problems, one fixture.

    **The suite must not need Redis.** `get_limiter()` builds a
    `RedisLimiter` by default, which opens a connection on first use.
    Leaving that in place made 34 route tests depend on a running Redis
    for a protection none of them is about.

    **Buckets must not leak between tests.** Register is limited to 3
    per minute per IP, and every test client reports the same IP. With
    one shared bucket, the fourth test that happens to register a user
    gets a 429 -- and which test that is depends on execution order, so
    the failure moves when a test is added anywhere in the suite. That
    is exactly what happened: `test_same_email_can_register_in_two_orgs`
    started failing because earlier tests had drained the bucket.

    Limiting stays ENABLED so the dependencies are exercised on every
    route; it is the STATE that is reset. Tests that assert limiting
    behaviour install their own limiter with a tiny limit.
    """
    from app.api import limits

    limits.set_limiter(limits.InMemoryLimiter())
    limits.reset_fallback()
    yield
    limits.set_limiter(None)
    limits.reset_fallback()


def _require_test_database_url() -> str:
    url = settings.TEST_DATABASE_URL
    if not url:
        pytest.skip(
            "TEST_DATABASE_URL is not set. Start the disposable database with "
            "`docker compose up -d test-db` and point TEST_DATABASE_URL at it "
            "(see .env.example)."
        )
    # Hard guard, not a convention: this fixture drops every table in the
    # target database. Pointing it at DATABASE_URL by accident would wipe
    # dev data, so refuse rather than trust the operator.
    if url == settings.DATABASE_URL:
        pytest.fail("TEST_DATABASE_URL must not equal DATABASE_URL -- the suite drops schema.")
    return url


@pytest.fixture(scope="session")
def engine():
    url = _require_test_database_url()
    eng = create_engine(url, pool_pre_ping=True)
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment problem, not a test failure
        pytest.skip(f"test database at {url} is not reachable: {exc}")

    # Full reset once per session: a leftover table or a half-applied
    # migration from a previous run would otherwise make results depend on
    # history instead of on the code under test.
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    alembic_cfg = Config(os.path.join(REPO_ROOT, "alembic.ini"))
    alembic_cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "alembic"))
    # env.py reads settings.DATABASE_URL; override it so migrations land in
    # the test database and never in dev.
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(alembic_cfg, "head")

    yield eng
    eng.dispose()


@pytest.fixture()
def db_session(engine) -> Session:
    """One test = one transaction, rolled back at the end.

    The session is bound to an outer transaction on a single connection
    rather than committing for real, so tests cannot leak rows into each
    other even though they share one migrated database. `session.commit()`
    inside application code commits the inner nested transaction (a
    SAVEPOINT); the outer one is still rolled back here.
    """
    connection = engine.connect()
    outer = connection.begin()
    TestingSessionLocal = sessionmaker(
        bind=connection, autocommit=False, autoflush=False, join_transaction_mode="create_savepoint"
    )
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture()
def client(db_session, monkeypatch, tmp_path):
    def override_get_db():
        # Deliberately does not close db_session: its lifecycle belongs to
        # the db_session fixture, so the test body and the route it calls
        # observe the same uncommitted transaction.
        yield db_session

    fastapi_app.dependency_overrides[get_db] = override_get_db

    # Uploads land in a per-test tmp dir so the suite never writes into the
    # repo's uploads/ directory. get_storage() is lru_cached, so the cache
    # must be cleared on both sides of the override or the first test to
    # touch storage would pin the root for the whole session.
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(settings, "STORAGE_LOCAL_ROOT", str(tmp_path / "storage"))
    get_storage.cache_clear()

    # ingest_document.delay() would publish to a real Redis broker. The
    # ingestion task has its own tests; here the enqueue is a no-op so the
    # HTTP-layer tests need no running worker.
    monkeypatch.setattr(document_service.ingest_document, "delay", lambda *a, **k: None)

    # The lifespan builds its OWN session -- `db = SessionLocal()` in
    # app/main.py -- which is not covered by the get_db override above.
    # Left alone, every startup check in the suite reads the DEV
    # database, so suite behaviour depends on whatever is sitting in a
    # developer's local Postgres.
    #
    # That is not hypothetical. Running the eval harness with
    # EMBEDDING_PROVIDER=fake writes chunks tagged `fake-embedding` into
    # the dev DB; the embedding-model guard then read them at startup,
    # set `embedding_model_error`, and four SSE tests plus the /health
    # test began failing with no change to any code they exercise. The
    # suite was green or red according to a database it does not own.
    #
    # `close` is swallowed because the session's lifecycle belongs to
    # the db_session fixture, which rolls the whole test back.
    import app.main as main_module

    class _SuiteSession:
        """The test session, minus the right to close it."""

        def __getattr__(self, name):
            return getattr(db_session, name)

        def close(self):
            return None

    monkeypatch.setattr(main_module, "SessionLocal", lambda: _SuiteSession())

    with TestClient(fastapi_app) as test_client:
        yield test_client

    fastapi_app.dependency_overrides.clear()
    get_storage.cache_clear()


@pytest.fixture()
def org_factory(db_session):
    def _make(name: str | None = None) -> Organization:
        # Unique suffix by default: organizations.name is UNIQUE now, so a
        # shared literal would make the second call in a test fail.
        org = Organization(id=uuid.uuid4(), name=name or f"Org-{uuid.uuid4().hex[:8]}")
        db_session.add(org)
        db_session.commit()
        return org

    return _make


@pytest.fixture()
def user_factory(db_session):
    def _make(
        org: Organization,
        role: UserRole = UserRole.MEMBER,
        email: str | None = None,
        password: str = "correct-horse-battery",
    ) -> User:
        user = User(
            id=uuid.uuid4(),
            org_id=org.id,
            email=email or f"{uuid.uuid4().hex[:8]}@example.com",
            hashed_password=hash_password(password),
            role=role,
        )
        db_session.add(user)
        db_session.commit()
        return user

    return _make


@pytest.fixture()
def auth_headers():
    def _make(user: User) -> dict:
        # Minted through the real token factory, not hand-rolled here, so a
        # change to claim names breaks the tests instead of silently
        # letting them pass against a shape production never issues.
        token = create_access_token(
            user_id=user.id, org_id=user.org_id, role=user.role.value
        )
        return {"Authorization": f"Bearer {token}"}

    return _make
