"""Importing this package registers every model on Base.metadata.

Alembic's env.py and the test fixtures both depend on that side effect: a
model that is never imported is invisible to autogenerate and to
create_all, which is how a table quietly goes missing from a migration.
"""
from app.db.base import Base
from app.models.api_key import ApiKey
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.organization import Organization
from app.models.user import User, UserRole

__all__ = [
    "Base",
    "ApiKey",
    "Organization",
    "User",
    "UserRole",
    "Document",
    "DocumentStatus",
    "DocumentChunk",
]
