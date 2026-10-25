import uuid

from app.models.document import Document, DocumentStatus
from app.models.user import UserRole


def test_upload_rejects_non_admin(client, org_factory, user_factory, auth_headers):
    org = org_factory()
    member = user_factory(org, role=UserRole.MEMBER)

    response = client.post(
        "/api/v1/documents",
        files={"file": ("doc.pdf", b"%PDF-1.4 fake pdf bytes", "application/pdf")},
        headers=auth_headers(member),
    )

    assert response.status_code == 403


def test_upload_rejects_invalid_file_type(client, org_factory, user_factory, auth_headers):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)

    response = client.post(
        "/api/v1/documents",
        files={"file": ("doc.exe", b"not a supported document type", "application/octet-stream")},
        headers=auth_headers(admin),
    )

    assert response.status_code == 400


def test_document_status_polling_returns_correct_status(
    client, db_session, org_factory, user_factory, auth_headers
):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)

    document = Document(
        id=uuid.uuid4(),
        org_id=org.id,
        uploaded_by=admin.id,
        filename="report.pdf",
        file_path="uploads/report.pdf",
        file_size_bytes=123,
        status=DocumentStatus.READY,
    )
    db_session.add(document)
    db_session.commit()

    response = client.get(f"/api/v1/documents/{document.id}", headers=auth_headers(admin))

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_cross_org_document_access_returns_404(
    client, db_session, org_factory, user_factory, auth_headers
):
    org_a = org_factory("Org A")
    org_b = org_factory("Org B")
    admin_a = user_factory(org_a, role=UserRole.ADMIN)
    admin_b = user_factory(org_b, role=UserRole.ADMIN)

    document = Document(
        id=uuid.uuid4(),
        org_id=org_a.id,
        uploaded_by=admin_a.id,
        filename="secret.pdf",
        file_path="uploads/secret.pdf",
        file_size_bytes=10,
        status=DocumentStatus.READY,
    )
    db_session.add(document)
    db_session.commit()

    response = client.get(f"/api/v1/documents/{document.id}", headers=auth_headers(admin_b))

    assert response.status_code == 404
