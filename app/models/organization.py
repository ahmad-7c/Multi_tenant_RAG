import uuid
from typing import TYPE_CHECKING
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# The relationship targets are declared as STRINGS and resolved by
# SQLAlchemy's declarative registry at mapper-configuration time, so
# importing them at runtime is unnecessary -- and would be a circular
# import. Declared under TYPE_CHECKING instead, which runs never:
# it tells a reader and a type checker where the name lives, and
# stops `Mapped["X"]` reading as a name that exists nowhere.
if TYPE_CHECKING:
    from app.models.user import User


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # UNIQUE because login resolves a user by (org name, email): a duplicate
    # org name would make that lookup ambiguous, and whichever row the DB
    # happened to return first would decide which tenant you logged into.
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # Bumped whenever this org's corpus changes (document ingested READY, or
    # document deleted). It is part of the answer-cache key, so a bump
    # invalidates every cached answer for this org at once without having to
    # enumerate and delete individual keys.
    index_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    users: Mapped[list["User"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
