from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.models.user import User
from app.schemas.user import UserResponse

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserResponse)
def read_me(current_user: User = Depends(get_current_user)):
    # The response_model is what keeps hashed_password out of the body --
    # returning the ORM object directly without it would serialize every
    # column on the model.
    return current_user
