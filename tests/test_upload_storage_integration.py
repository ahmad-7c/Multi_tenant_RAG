"""Upload -> storage wiring.

Proves the API actually persists bytes under the key it records on the
document row, which is the contract the worker depends on.
"""
from app.models.user import UserRole
from app.services.storage import get_storage
from app.services import document_service


def test_upload_writes_bytes_under_the_recorded_storage_key(
    client, db_session, org_factory, user_factory, auth_headers
):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)

    response = client.post(
        "/api/v1/documents",
        files={"file": ("notes.md", b"# Heading\n\nbody text", "text/markdown")},
        headers=auth_headers(admin),
    )
    assert response.status_code == 202

    document = document_service.get_document(
        db_session, org_id=org.id, document_id=response.json()["document_id"]
    )
    # The worker is given only this key -- if the bytes are not here under
    # exactly it, ingestion fails with an unreadable-file error.
    assert get_storage().read(document.storage_key) == b"# Heading\n\nbody text"


def test_storage_key_is_prefixed_by_org_and_document(
    client, db_session, org_factory, user_factory, auth_headers
):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)

    response = client.post(
        "/api/v1/documents",
        files={"file": ("notes.md", b"x", "text/markdown")},
        headers=auth_headers(admin),
    )
    document_id = response.json()["document_id"]
    document = document_service.get_document(
        db_session, org_id=org.id, document_id=document_id
    )

    assert document.storage_key == f"{org.id}/{document_id}/notes.md"


def test_hostile_filename_cannot_escape_the_storage_prefix(
    client, db_session, org_factory, user_factory, auth_headers
):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)

    response = client.post(
        "/api/v1/documents",
        files={"file": ("../../../../etc/passwd.md", b"x", "text/markdown")},
        headers=auth_headers(admin),
    )
    assert response.status_code == 202

    document_id = response.json()["document_id"]
    document = document_service.get_document(
        db_session, org_id=org.id, document_id=document_id
    )

    # Still inside this org's prefix, with every traversal segment gone.
    assert document.storage_key.startswith(f"{org.id}/{document_id}/")
    assert ".." not in document.storage_key
    # The original name is preserved on the row for display/citations even
    # though it was sanitized out of the key.
    assert document.filename == "../../../../etc/passwd.md"
