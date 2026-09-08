from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Roadmap, User
from app.schemas import RoadmapSummary
from app.security import get_current_user

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_model=list[RoadmapSummary])
def dashboard(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return (
        db.query(Roadmap)
        .filter(Roadmap.user_id == current_user.id)
        .order_by(Roadmap.created_at.desc())
        .all()
    )
