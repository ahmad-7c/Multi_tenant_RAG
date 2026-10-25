import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr

from app.models.user import UserRole


class UserResponse(BaseModel):
    """Response shape for a user.

    hashed_password is absent by construction, not filtered out later: the
    schema is an allowlist, so adding a column to the model can never leak
    it through this endpoint.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    email: EmailStr
    role: UserRole
    created_at: datetime
