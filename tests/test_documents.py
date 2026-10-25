"""Document endpoint tests (PART 5 + PART 6)."""
import uuid

from app.core.config import settings
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.organization import Organization
from app.models.user import UserRole
from app.services.storage import get_storage


def _doc(org, user, **overrides):
    document_id = overrides.pop("id", uuid.uuid4())
    return Document(
        id=document_id,
        org_id=org.id,
        uploaded_by=user.id,
        filename=overrides.pop("filename", "report.pdf"),
        storage_key=overrides.pop("storage_key", f"{org.id}/{document_id}/report.pdf"),
        file_size_bytes=overrides.pop("file_size_bytes", 123),
        status=overrides.pop("status", DocumentStatus.READY),
        **overrides,
    )


# ------------------------------------------------------------------ upload ----


def test_upload_rejects_non_admin(client, org_factory, user_factory, auth_headers):
    org = org_factory()
    member = user_factory(org, role=UserRole.MEMBER)

    response = client.post(
        "/api/v1/documents",
        files={"file": ("doc.pdf", b"%PDF-1.4 fake pdf bytes", "application/pdf")},
        headers=auth_headers(member),
    )

    assert response.status_code == 403


def test_upload_requires_authentication(client):
    response = client.post(
        "/api/v1/documents",
        files={"file": ("doc.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert response.status_code == 401


def test_upload_rejects_invalid_file_type(client, org_factory, user_factory, auth_headers):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)

    response = client.post(
        "/api/v1/documents",
        files={"file": ("doc.exe", b"not a supported document type", "application/octet-stream")},
        headers=auth_headers(admin),
    )

    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["detail"]


def test_upload_accepts_pdf_txt_and_md(client, org_factory, user_factory, auth_headers):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)

    for name, mime in [("a.pdf", "application/pdf"), ("b.txt", "text/plain"), ("c.md", "text/markdown")]:
        response = client.post(
            "/api/v1/documents",
            files={"file": (name, b"body content", mime)},
            headers=auth_headers(admin),
        )
        assert response.status_code == 202, name


def test_upload_returns_202_with_a_poll_url(client, org_factory, user_factory, auth_headers):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)

    response = client.post(
        "/api/v1/documents",
        files={"file": ("notes.md", b"# Title\n\nBody.", "text/markdown")},
        headers=auth_headers(admin),
    )

    # 202, not 201: the row exists but is not queryable yet. 201 would tell
    # the client the resource is ready and the first question would miss.
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["poll_url"] == f"/api/v1/documents/{body['document_id']}"


def test_upload_enforces_the_max_size(client, org_factory, user_factory, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 1024)
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)

    response = client.post(
        "/api/v1/documents",
        files={"file": ("big.txt", b"x" * 4096, "text/plain")},
        headers=auth_headers(admin),
    )

    # 413, not 400: the request is well-formed, just too large.
    assert response.status_code == 413


def test_upload_rejects_an_empty_file(client, org_factory, user_factory, auth_headers):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)

    response = client.post(
        "/api/v1/documents",
        files={"file": ("empty.txt", b"", "text/plain")},
        headers=auth_headers(admin),
    )

    assert response.status_code == 400


def test_upload_response_never_leaks_the_storage_key(
    client, org_factory, user_factory, auth_headers
):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)

    response = client.post(
        "/api/v1/documents",
        files={"file": ("notes.md", b"body", "text/markdown")},
        headers=auth_headers(admin),
    )

    # storage_key is the address of the object in the bucket; it has no
    # business reaching a client.
    assert "storage_key" not in response.text
    assert "org_id" not in response.text


# ------------------------------------------------------------------- read ----


def test_document_status_polling_returns_correct_status(
    client, db_session, org_factory, user_factory, auth_headers
):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)
    document = _doc(org, admin, status=DocumentStatus.READY)
    db_session.add(document)
    db_session.commit()

    response = client.get(f"/api/v1/documents/{document.id}", headers=auth_headers(admin))

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_failed_document_exposes_its_error_message(
    client, db_session, org_factory, user_factory, auth_headers
):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)
    document = _doc(
        org,
        admin,
        status=DocumentStatus.FAILED,
        error_message="No extractable text layer was found -- it needs OCR.",
    )
    db_session.add(document)
    db_session.commit()

    body = client.get(f"/api/v1/documents/{document.id}", headers=auth_headers(admin)).json()

    # This string is the only feedback the uploader gets about why their
    # document is unusable.
    assert "OCR" in body["error_message"]


def test_detail_reports_searchable_chunk_count(
    client, db_session, org_factory, user_factory, auth_headers
):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)
    document = _doc(org, admin)
    db_session.add(document)
    db_session.flush()

    parent = DocumentChunk(
        id=uuid.uuid4(),
        org_id=org.id,
        document_id=document.id,
        content="parent",
        page_number=1,
        token_count=1,
        chunk_index=0,
    )
    db_session.add(parent)
    db_session.flush()
    for i in range(3):
        db_session.add(
            DocumentChunk(
                id=uuid.uuid4(),
                org_id=org.id,
                document_id=document.id,
                parent_id=parent.id,
                content=f"child {i}",
                page_number=1,
                token_count=1,
                chunk_index=i,
            )
        )
    db_session.commit()

    body = client.get(f"/api/v1/documents/{document.id}", headers=auth_headers(admin)).json()

    # Children only: parents are context, not search targets, so this is
    # the number that says whether the document is findable.
    assert body["chunk_count"] == 3


def test_members_can_read_documents(client, db_session, org_factory, user_factory, auth_headers):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)
    member = user_factory(org, role=UserRole.MEMBER)
    document = _doc(org, admin)
    db_session.add(document)
    db_session.commit()

    # Reading is the whole point of the product for a member; only writing
    # is admin-gated.
    assert client.get(
        f"/api/v1/documents/{document.id}", headers=auth_headers(member)
    ).status_code == 200


def test_cross_org_document_access_returns_404(
    client, db_session, org_factory, user_factory, auth_headers
):
    org_a, org_b = org_factory(), org_factory()
    admin_a = user_factory(org_a, role=UserRole.ADMIN)
    admin_b = user_factory(org_b, role=UserRole.ADMIN)
    document = _doc(org_a, admin_a, filename="secret.pdf")
    db_session.add(document)
    db_session.commit()

    assert client.get(
        f"/api/v1/documents/{document.id}", headers=auth_headers(admin_b)
    ).status_code == 404


def test_unknown_document_id_returns_404(client, org_factory, user_factory, auth_headers):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)

    assert client.get(
        f"/api/v1/documents/{uuid.uuid4()}", headers=auth_headers(admin)
    ).status_code == 404


# ----------------------------------------------------------------- delete ----


def test_delete_requires_admin(client, db_session, org_factory, user_factory, auth_headers):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)
    member = user_factory(org, role=UserRole.MEMBER)
    document = _doc(org, admin)
    db_session.add(document)
    db_session.commit()

    assert client.delete(
        f"/api/v1/documents/{document.id}", headers=auth_headers(member)
    ).status_code == 403


def test_delete_removes_row_chunks_and_storage_object(
    client, db_session, org_factory, user_factory, auth_headers
):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)

    upload = client.post(
        "/api/v1/documents",
        files={"file": ("notes.md", b"# Title\n\nBody text.", "text/markdown")},
        headers=auth_headers(admin),
    )
    document_id = uuid.UUID(upload.json()["document_id"])
    document = db_session.get(Document, document_id)
    storage_key = document.storage_key
    assert get_storage().exists(storage_key)

    parent = DocumentChunk(
        id=uuid.uuid4(),
        org_id=org.id,
        document_id=document_id,
        content="parent",
        page_number=1,
        token_count=1,
        chunk_index=0,
    )
    db_session.add(parent)
    db_session.commit()

    response = client.delete(f"/api/v1/documents/{document_id}", headers=auth_headers(admin))

    assert response.status_code == 204
    assert db_session.get(Document, document_id) is None
    assert (
        db_session.query(DocumentChunk)
        .filter(DocumentChunk.document_id == document_id)
        .count()
        == 0
    )
    assert not get_storage().exists(storage_key)


def test_delete_increments_index_version(
    client, db_session, org_factory, user_factory, auth_headers
):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)
    document = _doc(org, admin)
    db_session.add(document)
    db_session.commit()
    before = db_session.get(Organization, org.id).index_version

    client.delete(f"/api/v1/documents/{document.id}", headers=auth_headers(admin))

    db_session.expire_all()
    # The corpus changed, so every cached answer for this org must stop
    # being served -- a cached answer citing a deleted document is worse
    # than a cache miss.
    assert db_session.get(Organization, org.id).index_version == before + 1


def test_delete_across_orgs_returns_404_and_changes_nothing(
    client, db_session, org_factory, user_factory, auth_headers
):
    org_a, org_b = org_factory(), org_factory()
    admin_a = user_factory(org_a, role=UserRole.ADMIN)
    admin_b = user_factory(org_b, role=UserRole.ADMIN)
    document = _doc(org_a, admin_a)
    db_session.add(document)
    db_session.commit()

    response = client.delete(f"/api/v1/documents/{document.id}", headers=auth_headers(admin_b))

    assert response.status_code == 404
    # Still there: a 404 must not be a successful delete with a misleading
    # status code.
    assert db_session.get(Document, document.id) is not None


def test_delete_unknown_id_returns_404(client, org_factory, user_factory, auth_headers):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)

    assert client.delete(
        f"/api/v1/documents/{uuid.uuid4()}", headers=auth_headers(admin)
    ).status_code == 404


def test_delete_succeeds_even_if_the_storage_object_is_already_gone(
    client, db_session, org_factory, user_factory, auth_headers
):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)
    upload = client.post(
        "/api/v1/documents",
        files={"file": ("notes.md", b"body", "text/markdown")},
        headers=auth_headers(admin),
    )
    document_id = uuid.UUID(upload.json()["document_id"])
    get_storage().delete(db_session.get(Document, document_id).storage_key)

    # The document is already gone from the user's point of view; raising
    # here would make a successful delete look like something to retry.
    response = client.delete(f"/api/v1/documents/{document_id}", headers=auth_headers(admin))

    assert response.status_code == 204
    assert db_session.get(Document, document_id) is None


# ------------------------------------------------------------------- list ----


def test_list_is_org_scoped(client, db_session, org_factory, user_factory, auth_headers):
    org_a, org_b = org_factory(), org_factory()
    admin_a = user_factory(org_a, role=UserRole.ADMIN)
    admin_b = user_factory(org_b, role=UserRole.ADMIN)
    db_session.add_all(
        [_doc(org_a, admin_a, filename="mine.pdf"), _doc(org_b, admin_b, filename="theirs.pdf")]
    )
    db_session.commit()

    listing = client.get("/api/v1/documents", headers=auth_headers(admin_a)).json()

    assert [d["filename"] for d in listing] == ["mine.pdf"]


def test_list_response_never_leaks_storage_keys(
    client, db_session, org_factory, user_factory, auth_headers
):
    org = org_factory()
    admin = user_factory(org, role=UserRole.ADMIN)
    db_session.add(_doc(org, admin))
    db_session.commit()

    response = client.get("/api/v1/documents", headers=auth_headers(admin))

    assert "storage_key" not in response.text
