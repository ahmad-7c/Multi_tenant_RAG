import enum
import uuid
from typing import TYPE_CHECKING
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# The relationship targets are declared as STRINGS and resolved by
# SQLAlchemy's declarative registry at mapper-configuration time, so
# importing them at runtime is unnecessary -- and would be a circular
# import. Declared under TYPE_CHECKING instead, which runs never:
# it tells a reader and a type checker where the name lives, and
# stops `Mapped["X"]` reading as a name that exists nowhere.
if TYPE_CHECKING:
    from app.models.organization import Organization


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    MEMBER = "member"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Same email can exist across different orgs, so uniqueness is
        # scoped to (org_id, email) rather than email alone.
        UniqueConstraint("org_id", "email", name="uq_users_org_id_email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    # values_callable is required, not cosmetic: by default SQLAlchemy
    # persists a Python enum's *name* ("MEMBER"), while the Postgres type
    # created by migration 0001 holds its *value* ("member"). Without this
    # every insert fails with "invalid input value for enum user_role".
    # (SQLite accepts either, which is why this only surfaces on Postgres.)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=UserRole.MEMBER,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    organization: Mapped["Organization"] = relationship(back_populates="users")
