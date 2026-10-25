import uuid
from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext

from app.core.config import settings

# bcrypt only. bcrypt==4.0.1 is pinned deliberately: passlib 1.7.4 reads
# bcrypt.__about__.__version__, which 4.1+ removed, so a newer bcrypt makes
# passlib log a spurious error and (on some paths) fail outright.
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# bcrypt silently truncates input at 72 bytes. Rejecting longer passwords
# up front is honest; accepting them would mean two different passwords
# over 72 bytes authenticate the same account.
BCRYPT_MAX_BYTES = 72


class InvalidTokenError(Exception):
    pass


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd_context.verify(plain, hashed)
    except ValueError:
        # A malformed/garbage hash in the DB must read as "wrong password",
        # not blow up into a 500 that tells an attacker something is odd
        # about this particular account.
        return False


def create_access_token(*, user_id: uuid.UUID, org_id: uuid.UUID, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        # org_id is minted into the token here and read back out only from
        # the verified payload -- it is never accepted from a request body,
        # query param or header anywhere in the app.
        "org_id": str(org_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            # Pinned explicitly: without require=["exp"] a token minted
            # without an exp claim would verify forever.
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc
