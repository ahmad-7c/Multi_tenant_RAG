from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.api.limits import limit_default
from app.api.deps import get_current_user, require_admin
from app.db.session import get_db
from app.models.user import User
from app.schemas.document import (
    DocumentDetailResponse,
    DocumentListItem,
    DocumentUploadResponse,
)
from app.services import document_service

router = APIRouter(prefix="/documents", tags=["documents"])

# 404, not 403, for a document belonging to another org. A 403 confirms
# that a document with this id exists somewhere, which turns the endpoint
# into an oracle for enumerating ids across tenants. 404 is
# indistinguishable from "never existed".
_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")


# Upload is rate limited on the DEFAULT bucket, not the answer one: it
# costs a write and a Celery enqueue, and the expensive part (embedding)
# happens in the worker where wall-clock is free and EMBED_REQUESTS_PER_
# MINUTE already throttles the provider.
@router.post(
    "",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(limit_default)],
)
async def upload_document(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    # Admin-gated: uploading changes what every member of the org can be
    # told by the system, so it is not a member-level action.
    current_user: User = Depends(require_admin),
):
    content = await document_service.read_upload_within_limit(file)
    document = document_service.create_document(
        db,
        # org_id comes from the verified token via get_current_user, never
        # from the request body. An org_id field in the multipart payload
        # is simply ignored.
        org_id=current_user.org_id,
        uploaded_by=current_user.id,
        filename=file.filename,
        content=content,
    )
    # 202, not 201: the document row exists but is not usable yet. Claiming
    # 201 Created would tell the client the resource is ready to query,
    # and the first question against it would return nothing.
    return DocumentUploadResponse(
        document_id=document.id,
        status=document.status,
        poll_url=f"/api/v1/documents/{document.id}",
    )


@router.get(
    "",
    response_model=list[DocumentListItem],
    dependencies=[Depends(limit_default)],
)
def list_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return document_service.list_documents(db, org_id=current_user.org_id)


@router.get(
    "/{document_id}",
    response_model=DocumentDetailResponse,
    dependencies=[Depends(limit_default)],
)
def get_document(
    document_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    document = document_service.get_document(
        db, org_id=current_user.org_id, document_id=document_id
    )
    if document is None:
        raise _NOT_FOUND

    response = DocumentDetailResponse.model_validate(document)
    response.chunk_count = document_service.count_searchable_chunks(
        db, org_id=current_user.org_id, document_id=document_id
    )
    return response


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(limit_default)],
)
def delete_document(
    document_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    deleted = document_service.delete_document(
        db, org_id=current_user.org_id, document_id=document_id
    )
    if not deleted:
        raise _NOT_FOUND
    # 204 with no body: there is nothing meaningful left to describe.
    return Response(status_code=status.HTTP_204_NO_CONTENT)
