from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.limits import limit_login, limit_register
from app.db.session import get_db
from app.schemas.auth import LoginRequest, RegisterRequest, RegisterResponse, TokenResponse
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


# Routes stay thin: validate via the schema, call the service, shape the
# response. No business logic here.
#
# The limit is per CLIENT IP, not per org: there is no org until this
# call succeeds, and the abuse this endpoint attracts is one source
# creating many accounts.
@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_register)],
)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    user = auth_service.register(db, payload)
    return RegisterResponse(
        org_id=user.org_id, user_id=user.id, email=user.email, role=user.role
    )


# Also per IP. A per-ACCOUNT limit cannot see credential stuffing, which
# tries one password against ten thousand accounts and never trips a
# counter on any single one of them.
@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[Depends(limit_login)],
)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    return TokenResponse(access_token=auth_service.login(db, payload))
