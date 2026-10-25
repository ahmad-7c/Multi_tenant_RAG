from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.auth import LoginRequest, RegisterRequest, RegisterResponse, TokenResponse
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


# Routes stay thin: validate via the schema, call the service, shape the
# response. No business logic here.
@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    user = auth_service.register(db, payload)
    return RegisterResponse(
        org_id=user.org_id, user_id=user.id, email=user.email, role=user.role
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    return TokenResponse(access_token=auth_service.login(db, payload))
