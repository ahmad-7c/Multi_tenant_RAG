from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.security import InvalidTokenError, decode_access_token
from app.db.session import get_db
from app.models.user import User, UserRole

# tokenUrl is documentation-only here (no /auth router exists yet); it just
# tells the OpenAPI UI where a token would come from.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
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

    # org_id is read ONLY from the verified JWT payload -- never from a
    # request body, query param, or header. That's the entire tenancy
    # boundary: everything downstream trusts this value.
    user_id = payload.get("sub")
    org_id = payload.get("org_id")
    if user_id is None or org_id is None:
        raise credentials_exception

    user = db.get(User, UUID(user_id))
    # Re-checking org_id against the user's current row (not just trusting
    # the token's claim) catches a token minted before a user was moved
    # between orgs -- a narrow case, but cheap to guard against here.
    if user is None or str(user.org_id) != org_id:
        raise credentials_exception

    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required"
        )
    return user
