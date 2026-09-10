from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Notification, NotificationPreference, User
from app.schemas import (
    NotificationOut,
    NotificationPreferenceOut,
    NotificationPreferenceUpdate,
)
from app.security import get_current_user

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/preferences", response_model=NotificationPreferenceOut)
def get_preferences(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    pref = (
        db.query(NotificationPreference)
        .filter(NotificationPreference.user_id == current_user.id)
        .first()
    )
    if pref is None:
        pref = NotificationPreference(
            user_id=current_user.id, email_enabled=True, milestone_notifications=True
        )
        db.add(pref)
        db.commit()
        db.refresh(pref)
    return pref


@router.patch("/preferences", response_model=NotificationPreferenceOut)
def update_preferences(
    payload: NotificationPreferenceUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pref = (
        db.query(NotificationPreference)
        .filter(NotificationPreference.user_id == current_user.id)
        .first()
    )
    if pref is None:
        pref = NotificationPreference(
            user_id=current_user.id, email_enabled=True, milestone_notifications=True
        )
        db.add(pref)

    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(pref, key, value)

    db.commit()
    db.refresh(pref)
    return pref


@router.get("", response_model=list[NotificationOut])
def get_notifications(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    items = (
        db.query(Notification)
        .filter(Notification.user_id == current_user.id, Notification.delivered.is_(False))
        .order_by(Notification.created_at.asc())
        .all()
    )
    out = [NotificationOut.model_validate(n) for n in items]
    for n in items:
        n.delivered = True
    if items:
        db.commit()
    return out
