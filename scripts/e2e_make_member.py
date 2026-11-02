"""Create a MEMBER user in an existing organization. E2E fixture only.

There is no invite endpoint yet -- registration always creates the
organization's first admin -- so a role test cannot produce a member
through the product. Rather than add an unused invite API just to make
a test possible, this writes the row directly.

    DM_MEMBER_PASSWORD=... python scripts/e2e_make_member.py <org> <email>

Refuses unless ENV is a development value and DATABASE_URL is local,
on the same reasoning as scripts/dev_set_password.py.
"""
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def main() -> int:
    if settings.ENV not in ("development", "dev", "local", "test"):
        print(f"refusing to run with ENV={settings.ENV!r}")
        return 2
    if not any(h in settings.DATABASE_URL for h in LOCAL_HOSTS):
        print("refusing to run: DATABASE_URL does not look local")
        return 2
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    org_name, email = sys.argv[1], sys.argv[2]
    password = os.environ.get("DM_MEMBER_PASSWORD")
    if not password or len(password) < 8:
        print("set DM_MEMBER_PASSWORD (8+ characters)")
        return 2

    db = SessionLocal()
    try:
        org = db.execute(
            select(Organization).where(Organization.name == org_name)
        ).scalar_one_or_none()
        if org is None:
            print(f"no organization {org_name!r}")
            return 1

        db.add(
            User(
                id=uuid.uuid4(),
                org_id=org.id,
                email=email,
                hashed_password=hash_password(password),
                role=UserRole.MEMBER,
            )
        )
        db.commit()
        print(f"member {email} created in {org_name}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
