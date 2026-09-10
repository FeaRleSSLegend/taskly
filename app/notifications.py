import logging
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Notification, NotificationPreference, NotificationType, User

logger = logging.getLogger(__name__)


def send_email(to_email: str, subject: str, message: str) -> None:
    """Send an email using Resend.

    If no API key is configured or sending fails, logs the issue and continues
    without raising an exception.
    """
    settings = get_settings()
    if not settings.resend_api_key:
        logger.info("RESEND_API_KEY is not configured; skipping email to %s", to_email)
        return

    try:
        import resend

        resend.api_key = settings.resend_api_key
        resend.Emails.send(
            {
                "from": "onboarding@resend.dev",
                "to": to_email,
                "subject": subject,
                "text": message,
                "html": f"<p>{message}</p>",
            }
        )
    except Exception as exc:
        logger.exception("Failed to send email to %s: %s", to_email, exc)


def notify_user(
    db: Session,
    user: User,
    notification_type: NotificationType,
    message: str,
    roadmap_id: UUID | None = None,
) -> Notification | None:
    """Create in-app Notification and send email if enabled by user preferences."""
    pref = (
        db.query(NotificationPreference)
        .filter(NotificationPreference.user_id == user.id)
        .first()
    )
    if pref is None:
        pref = NotificationPreference(
            user_id=user.id, email_enabled=True, milestone_notifications=True
        )
        db.add(pref)
        db.flush()

    if not pref.milestone_notifications:
        return None

    notification = Notification(
        user_id=user.id,
        type=notification_type,
        message=message,
        roadmap_id=roadmap_id,
        delivered=False,
    )
    db.add(notification)
    db.flush()

    if pref.email_enabled:
        send_email(user.email, message, message)

    return notification
