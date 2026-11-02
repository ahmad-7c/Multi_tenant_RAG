"""Set a local account's password, for development only.

Why this exists: accounts created while testing are real bcrypt
hashes, so a forgotten password cannot be recovered -- only replaced.
The alternative was writing test credentials into a file in the repo,
which is how a throwaway password ends up committed.

The new password is read from an ENVIRONMENT VARIABLE rather than a
command-line argument, because argv is visible to every other process
on the machine through the process list and lands in shell history.

    DM_NEW_PASSWORD=... python scripts/dev_set_password.py <org> <email>

Refuses to run unless ENV is a development value, and refuses to touch
anything if DATABASE_URL points somewhere that is not obviously local.
Both guards are crude on purpose: this is a tool that rewrites a
credential, and a crude guard that fires is better than a clever one
that does not.
"""
import os
import sys
from pathlib import Path

# Run from anywhere: `python scripts/dev_set_password.py` puts
# scripts/ on sys.path, not the repo root, so `import app` would fail
# unless the caller also set PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.security import BCRYPT_MAX_BYTES, hash_password  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.user import User  # noqa: E402

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def main() -> int:
    if settings.ENV not in ("development", "dev", "local", "test"):
        print(f"refusing to run with ENV={settings.ENV!r}; development only")
        return 2

    if not any(host in settings.DATABASE_URL for host in LOCAL_HOSTS):
        print("refusing to run: DATABASE_URL does not look local")
        return 2

    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    org_name, email = sys.argv[1], sys.argv[2]
    password = os.environ.get("DM_NEW_PASSWORD")
    if not password:
        print("set DM_NEW_PASSWORD in the environment first")
        return 2
    if len(password) < 8:
        print("password must be at least 8 characters")
        return 2
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        # bcrypt truncates silently past 72 bytes, so a longer password
        # would appear to work and then accept a shorter prefix too.
        print(f"password must be at most {BCRYPT_MAX_BYTES} bytes")
        return 2

    db = SessionLocal()
    try:
        user = db.execute(
            select(User)
            .join(Organization, Organization.id == User.org_id)
            .where(Organization.name == org_name, User.email == email)
        ).scalar_one_or_none()

        if user is None:
            print(f"no user {email!r} in organization {org_name!r}")
            return 1

        user.hashed_password = hash_password(password)
        db.commit()
        # The password itself is never echoed back.
        print(f"password updated for {email} in {org_name} (role: {user.role.value})")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
