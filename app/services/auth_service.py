import uuid

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import create_access_token, hash_password, verify_password
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.schemas.auth import LoginRequest, RegisterRequest


def register(db: Session, payload: RegisterRequest) -> User:
    """Create an organization and its first admin in one transaction.

    Org and user are created together on purpose: an org with no users is
    unreachable (nobody can log into it) and would permanently occupy its
    unique name.
    """
    # id generated here, not left to the column's default=uuid.uuid4: that
    # default only fires at INSERT time, so org.id would still be None when
    # it is read below for user.org_id -- producing a NOT NULL violation
    # that looks exactly like a uniqueness conflict from the outside.
    org = Organization(id=uuid.uuid4(), name=payload.org_name.strip())
    user = User(
        id=uuid.uuid4(),
        org_id=org.id,
        email=payload.email.lower(),
        hashed_password=hash_password(payload.password),
        # First user of a new org must be an admin, otherwise the org has
        # nobody who can upload documents.
        role=UserRole.ADMIN,
    )
    db.add(org)
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Matched on the constraint name rather than treating *any*
        # IntegrityError as a name conflict: a NOT NULL or FK violation
        # here is a bug in this function, and reporting it as 409
        # "name already exists" would hide it behind a plausible-looking
        # client error (which is exactly how it was hidden before).
        if "uq_organizations_name" not in str(getattr(exc, "orig", exc)):
            raise
        # 409, not 400: the request was well-formed, it just conflicts with
        # existing state.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An organization with that name already exists",
        )
    db.refresh(user)
    return user


def login(db: Session, payload: LoginRequest) -> str:
    user = (
        db.query(User)
        .join(Organization, User.org_id == Organization.id)
        .filter(
            Organization.name == payload.org_name.strip(),
            User.email == payload.email.lower(),
        )
        .one_or_none()
    )

    # Identical 401 whether the org is unknown, the email is unknown, or the
    # password is wrong -- distinguishing them turns this endpoint into an
    # account-enumeration oracle.
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
    )
    if user is None:
        # Still run a hash comparison against a throwaway value so a
        # nonexistent account doesn't answer measurably faster than a real
        # one with a bad password.
        verify_password(payload.password, hash_password("timing-equalizer"))
        raise invalid
    if not verify_password(payload.password, user.hashed_password):
        raise invalid

    return create_access_token(user_id=user.id, org_id=user.org_id, role=user.role.value)
