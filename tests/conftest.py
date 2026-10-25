import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 -- side-effect import, registers all models on Base.metadata
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.services import document_service


@pytest.fixture()
def db_session():
    # In-memory SQLite, not the real Postgres+pgvector stack: these tests
    # exercise the HTTP/service/auth layer added in Part 4, not pgvector-
    # specific behavior, so a disposable in-process DB keeps them fast and
    # dependency-free. StaticPool + check_same_thread=False makes every
    # session share the one in-memory connection instead of each getting
    # its own (and therefore empty) database.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session, monkeypatch, tmp_path):
    def override_get_db():
        # Deliberately does not close db_session here -- its lifecycle is
        # owned by the db_session fixture so the test body and the route
        # it calls share one open session/transaction.
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    # Uploaded files go under a per-test tmp dir instead of the real
    # uploads/ directory, so running the suite doesn't litter the repo.
    monkeypatch.setattr(document_service, "UPLOAD_DIR", tmp_path / "uploads")

    # ingest_document.delay() would otherwise try to publish to a real
    # Redis broker. These tests cover the HTTP/DB layer only -- the
    # ingestion task itself is exercised separately -- so the enqueue call
    # is replaced with a no-op.
    monkeypatch.setattr(document_service.ingest_document, "delay", lambda *a, **k: None)

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture()
def org_factory(db_session):
    def _make(name: str = "Org") -> Organization:
        org = Organization(id=uuid.uuid4(), name=name)
        db_session.add(org)
        db_session.commit()
        return org

    return _make


@pytest.fixture()
def user_factory(db_session):
    def _make(org: Organization, role: UserRole = UserRole.MEMBER, email: str | None = None) -> User:
        user = User(
            id=uuid.uuid4(),
            org_id=org.id,
            email=email or f"{uuid.uuid4()}@example.com",
            # Never a real hash in tests -- nothing here exercises login,
            # so there's no password to verify.
            hashed_password="unused-in-these-tests",
            role=role,
        )
        db_session.add(user)
        db_session.commit()
        return user

    return _make


@pytest.fixture()
def auth_headers():
    def _make(user: User) -> dict:
        token = jwt.encode(
            {
                "sub": str(user.id),
                "org_id": str(user.org_id),
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )
        return {"Authorization": f"Bearer {token}"}

    return _make
