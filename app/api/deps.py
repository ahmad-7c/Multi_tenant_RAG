from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.security import InvalidTokenError, decode_access_token
from app.db.session import get_db
from app.models.user import User, UserRole

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def get_current_user(
    token: str | None = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if token is None:
        raise credentials_exception

    try:
        payload = decode_access_token(token)
    except InvalidTokenError:
        raise credentials_exception

    # Everything from here down parses *attacker-supplied* claim values.
    # A missing key, a non-string, or a string that isn't a UUID must all
    # come back as 401 -- never a 500. A 500 is both a worse answer (it
    # tells the caller they found an unhandled edge) and a liability: the
    # same code path would otherwise raise ValueError/TypeError out of
    # UUID() and leak a stack trace into the logs on every probe.
    try:
        user_id = UUID(str(payload["sub"]))
        org_id = UUID(str(payload["org_id"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise credentials_exception

    # org_id is read ONLY from the verified JWT payload. That single fact is
    # the entire tenancy boundary: everything downstream trusts this value.
    user = db.get(User, user_id)
    # Re-checking org_id against the user's current row (rather than just
    # trusting the claim) catches a still-valid token minted before a user
    # was moved or removed from an org.
    if user is None or user.org_id != org_id:
        raise credentials_exception

    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required"
        )
    return user
