import uuid
from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import settings
from app.models.user import UserRole


def test_register_creates_org_and_admin(client):
    response = client.post(
        "/api/v1/auth/register",
        json={"org_name": "Acme Corp", "email": "Admin@Acme.com", "password": "s3cret-password"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["role"] == "admin"
    # Email is normalized to lowercase on write, so login is not
    # case-sensitive by accident of how the user typed it.
    assert body["email"] == "admin@acme.com"


def test_register_duplicate_org_name_returns_409(client):
    payload = {"org_name": "Acme Corp", "email": "a@acme.com", "password": "s3cret-password"}
    assert client.post("/api/v1/auth/register", json=payload).status_code == 201

    payload["email"] = "b@acme.com"
    response = client.post("/api/v1/auth/register", json=payload)

    assert response.status_code == 409


def test_register_response_never_exposes_password(client):
    response = client.post(
        "/api/v1/auth/register",
        json={"org_name": "Acme Corp", "email": "a@acme.com", "password": "s3cret-password"},
    )

    raw = response.text
    assert "hashed_password" not in raw
    assert "s3cret-password" not in raw


def test_login_returns_token_and_me_resolves_user(client):
    client.post(
        "/api/v1/auth/register",
        json={"org_name": "Acme Corp", "email": "a@acme.com", "password": "s3cret-password"},
    )

    login = client.post(
        "/api/v1/auth/login",
        json={"org_name": "Acme Corp", "email": "a@acme.com", "password": "s3cret-password"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    me = client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "a@acme.com"
    assert body["role"] == "admin"
    assert "hashed_password" not in body


def test_login_wrong_password_returns_401(client):
    client.post(
        "/api/v1/auth/register",
        json={"org_name": "Acme Corp", "email": "a@acme.com", "password": "s3cret-password"},
    )

    response = client.post(
        "/api/v1/auth/login",
        json={"org_name": "Acme Corp", "email": "a@acme.com", "password": "wrong-password"},
    )

    assert response.status_code == 401


def test_login_unknown_org_returns_401_not_404(client):
    # Same status and body as a wrong password: distinguishing "no such
    # org" from "wrong password" turns login into an enumeration oracle.
    response = client.post(
        "/api/v1/auth/login",
        json={"org_name": "Nonexistent", "email": "a@acme.com", "password": "s3cret-password"},
    )

    assert response.status_code == 401


def test_same_email_can_register_in_two_orgs(client):
    first = client.post(
        "/api/v1/auth/register",
        json={"org_name": "Org One", "email": "shared@example.com", "password": "s3cret-password"},
    )
    second = client.post(
        "/api/v1/auth/register",
        json={"org_name": "Org Two", "email": "shared@example.com", "password": "s3cret-password"},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["org_id"] != second.json()["org_id"]


def test_me_requires_a_token(client):
    assert client.get("/api/v1/users/me").status_code == 401


def _token(claims: dict) -> str:
    return jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def _exp() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=1)


def test_malformed_claims_return_401_never_500(client, org_factory, user_factory):
    """Any attacker-controlled claim shape must produce 401, not a 500.

    These all reach the claim-parsing code *after* signature verification,
    so they are exactly the inputs someone with a leaked signing key -- or
    a bug in token minting -- would produce.
    """
    org = org_factory()
    user = user_factory(org)

    bad_tokens = [
        # org_id claim entirely absent
        _token({"sub": str(user.id), "exp": _exp()}),
        # sub present but not a UUID
        _token({"sub": "not-a-uuid", "org_id": str(org.id), "exp": _exp()}),
        # org_id present but not a UUID
        _token({"sub": str(user.id), "org_id": "not-a-uuid", "exp": _exp()}),
        # claims of the wrong JSON type entirely
        _token({"sub": 12345, "org_id": None, "exp": _exp()}),
        _token({"sub": {"nested": "object"}, "org_id": ["a", "list"], "exp": _exp()}),
        # well-formed UUIDs that simply don't exist
        _token({"sub": str(uuid.uuid4()), "org_id": str(uuid.uuid4()), "exp": _exp()}),
        # real user, but the token claims a different org than their row
        _token({"sub": str(user.id), "org_id": str(uuid.uuid4()), "exp": _exp()}),
    ]

    for token in bad_tokens:
        response = client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401, f"expected 401, got {response.status_code}"


def test_token_missing_exp_is_rejected(client, org_factory, user_factory):
    org = org_factory()
    user = user_factory(org)
    token = _token({"sub": str(user.id), "org_id": str(org.id)})

    assert client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 401


def test_expired_token_is_rejected(client, org_factory, user_factory):
    org = org_factory()
    user = user_factory(org)
    token = _token(
        {
            "sub": str(user.id),
            "org_id": str(org.id),
            "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
        }
    )

    assert client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 401


def test_token_signed_with_wrong_secret_is_rejected(client, org_factory, user_factory):
    org = org_factory()
    user = user_factory(org)
    token = jwt.encode(
        {"sub": str(user.id), "org_id": str(org.id), "exp": _exp()},
        "a-completely-different-secret",
        algorithm=settings.JWT_ALGORITHM,
    )

    assert client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 401


def test_member_role_is_not_admin(client, org_factory, user_factory, auth_headers):
    org = org_factory()
    member = user_factory(org, role=UserRole.MEMBER)

    me = client.get("/api/v1/users/me", headers=auth_headers(member))

    assert me.status_code == 200
    assert me.json()["role"] == "member"
