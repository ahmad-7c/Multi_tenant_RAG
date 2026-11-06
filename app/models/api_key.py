import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
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


class ApiKey(Base):
    """A long-lived credential for server-to-server callers.

    Same tenancy rule as a JWT: org_id comes from the stored key row, never
    from the request. Issuing/rotating endpoints are out of scope for
    Phases 1-3 -- the table and the lookup shape exist so that adding them
    later is not a migration.
    """

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Which user minted the key, for audit. Nullable so revoking a user
    # doesn't force deleting their keys.
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # The raw key is shown once at creation and never stored, same rule as
    # a password. Only the hash lives here.
    hashed_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # Short non-secret identifier (e.g. "dm_live_a1b2") carried in the
    # presented key, so lookup is an indexed point read instead of bcrypt-
    # verifying the candidate against every key row in the table.
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)

    # Revoked rather than deleted: an audit trail of what a key did is
    # worthless if the key row it points at can vanish.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    organization: Mapped["Organization"] = relationship()
