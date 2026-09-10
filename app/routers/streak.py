from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, UserStreak
from app.schemas import UserStreakOut
from app.security import get_current_user

router = APIRouter(prefix="/streak", tags=["streak"])


@router.get("", response_model=UserStreakOut)
def get_streak(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    streak = db.query(UserStreak).filter(UserStreak.user_id == current_user.id).first()
    if streak is None:
        streak = UserStreak(user_id=current_user.id, current_streak=0, longest_streak=0)
        db.add(streak)
        db.commit()
        db.refresh(streak)
    return streak
