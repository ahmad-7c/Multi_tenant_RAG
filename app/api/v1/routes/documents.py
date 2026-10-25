from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.db.session import get_db
from app.models.user import User
from app.schemas.document import DocumentDetailResponse, DocumentListItem, DocumentUploadResponse
from app.services import document_service

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("", response_model=DocumentUploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    content = await file.read()
    document = document_service.create_document(
        db,
        org_id=current_user.org_id,
        uploaded_by=current_user.id,
        filename=file.filename,
        content=content,
    )
    return DocumentUploadResponse(document_id=document.id, status=document.status)


@router.get("", response_model=list[DocumentListItem])
def list_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return document_service.list_documents(db, org_id=current_user.org_id)


@router.get("/{document_id}", response_model=DocumentDetailResponse)
def get_document(
    document_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    document = document_service.get_document(db, org_id=current_user.org_id, document_id=document_id)
    if document is None:
        # 404, not 403: a 403 would confirm a document with this id exists
        # (just in another org), letting someone probe ids across tenants.
        # 404 is indistinguishable from "never existed".
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document
