import uuid

from pydantic import BaseModel, EmailStr, Field

from app.models.user import UserRole


class RegisterRequest(BaseModel):
    """Registers a brand-new organization plus its first (admin) user.

    There is deliberately no org_id field. A caller cannot choose which
    tenant they join -- that would be a trivial cross-tenant escalation.
    """

    org_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    # 72 bytes is bcrypt's hard truncation point; see security.BCRYPT_MAX_BYTES.
    password: str = Field(min_length=8, max_length=72)


class RegisterResponse(BaseModel):
    org_id: uuid.UUID
    user_id: uuid.UUID
    email: EmailStr
    role: UserRole


class LoginRequest(BaseModel):
    # Login is scoped by org name because (org_id, email) is the uniqueness
    # key on users -- the same email may legitimately exist in several orgs,
    # so an email alone does not identify an account.
    org_name: str
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
