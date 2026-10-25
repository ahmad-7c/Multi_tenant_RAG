"""Tenant-isolation tests.

These are the tests that matter most in this system: every other bug
produces a bad answer, an isolation bug produces someone else's answer.
"""
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.document import Document, DocumentStatus
from app.models.organization import Organization
from app.models.user import User, UserRole


def test_org_name_is_unique_at_the_database_level(db_session):
    # Enforced by a DB constraint, not only by the service: a second API
    # instance racing the same name must lose at the database, since the
    # service's "does this name exist" check cannot be atomic on its own.
    db_session.add(Organization(id=uuid.uuid4(), name="Duplicate Inc"))
    db_session.commit()

    db_session.add(Organization(id=uuid.uuid4(), name="Duplicate Inc"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_same_email_unique_per_org_not_globally(db_session):
    org_a = Organization(id=uuid.uuid4(), name=f"A-{uuid.uuid4().hex[:6]}")
    org_b = Organization(id=uuid.uuid4(), name=f"B-{uuid.uuid4().hex[:6]}")
    db_session.add_all([org_a, org_b])
    db_session.commit()

    db_session.add_all(
        [
            User(id=uuid.uuid4(), org_id=org_a.id, email="x@y.com", hashed_password="h"),
            User(id=uuid.uuid4(), org_id=org_b.id, email="x@y.com", hashed_password="h"),
        ]
    )
    db_session.commit()  # allowed: uniqueness is (org_id, email)

    db_session.add(User(id=uuid.uuid4(), org_id=org_a.id, email="x@y.com", hashed_password="h"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_list_documents_only_returns_own_org(
    client, db_session, org_factory, user_factory, auth_headers
):
    org_a, org_b = org_factory(), org_factory()
    admin_a = user_factory(org_a, role=UserRole.ADMIN)
    admin_b = user_factory(org_b, role=UserRole.ADMIN)

    db_session.add_all(
        [
            Document(
                id=uuid.uuid4(),
                org_id=org_a.id,
                uploaded_by=admin_a.id,
                filename="a.pdf",
                storage_key="org/doc/a.pdf",
                file_size_bytes=1,
                status=DocumentStatus.READY,
            ),
            Document(
                id=uuid.uuid4(),
                org_id=org_b.id,
                uploaded_by=admin_b.id,
                filename="b.pdf",
                storage_key="org/doc/b.pdf",
                file_size_bytes=1,
                status=DocumentStatus.READY,
            ),
        ]
    )
    db_session.commit()

    listing = client.get("/api/v1/documents", headers=auth_headers(admin_a)).json()

    assert [d["filename"] for d in listing] == ["a.pdf"]


def test_cross_org_document_fetch_returns_404(
    client, db_session, org_factory, user_factory, auth_headers
):
    org_a, org_b = org_factory(), org_factory()
    admin_a = user_factory(org_a, role=UserRole.ADMIN)
    admin_b = user_factory(org_b, role=UserRole.ADMIN)

    doc = Document(
        id=uuid.uuid4(),
        org_id=org_a.id,
        uploaded_by=admin_a.id,
        filename="secret.pdf",
        storage_key="org/doc/secret.pdf",
        file_size_bytes=10,
        status=DocumentStatus.READY,
    )
    db_session.add(doc)
    db_session.commit()

    # 404 and not 403: a 403 confirms the id exists somewhere, which lets
    # someone enumerate ids across tenants. 404 is indistinguishable from
    # "never existed".
    assert client.get(
        f"/api/v1/documents/{doc.id}", headers=auth_headers(admin_b)
    ).status_code == 404


def test_org_id_in_request_body_cannot_override_the_token(
    client, org_factory, user_factory, auth_headers
):
    """org_id is taken from the JWT only.

    Sending an org_id in the multipart body must have no effect -- the
    uploaded document must still belong to the token's org.
    """
    org_a, org_b = org_factory(), org_factory()
    admin_a = user_factory(org_a, role=UserRole.ADMIN)
    user_factory(org_b, role=UserRole.ADMIN)

    response = client.post(
        "/api/v1/documents",
        files={"file": ("note.txt", b"hello world", "text/plain")},
        data={"org_id": str(org_b.id)},
        headers=auth_headers(admin_a),
    )
    assert response.status_code == 202
    document_id = response.json()["document_id"]

    # Visible to org A...
    assert client.get(
        f"/api/v1/documents/{document_id}", headers=auth_headers(admin_a)
    ).status_code == 200
    # ...and not to the org named in the body.
    admin_b = user_factory(org_b, role=UserRole.ADMIN)
    assert client.get(
        f"/api/v1/documents/{document_id}", headers=auth_headers(admin_b)
    ).status_code == 404
