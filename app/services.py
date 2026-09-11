"""Shared service functions called by both REST routes and the Nodi chat tools.

All functions here are plain Python — no FastAPI request/response handling.
They accept a SQLAlchemy Session and whatever arguments are needed, and return
plain data (dicts, ORM objects, or structured dicts for the chat tools).

Keeping the logic here ensures the chat feature calls the exact same code path
as the REST API; there is no parallel implementation of roadmap creation, node
completion, or progress calculation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from app.models import Node, Notification, NotificationPreference, NotificationType, Roadmap, RoadmapStatus, User, UserStreak

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Streak & milestone helpers (moved from app/routers/nodes.py so that both
# the REST route and the chat tool's complete_node_by_name call the same code)
# ---------------------------------------------------------------------------


def update_streak(db: Session, user_id: UUID) -> None:
    """Increment / reset the user's activity streak based on UTC date."""
    today = datetime.now(timezone.utc).date()
    streak = db.query(UserStreak).filter(UserStreak.user_id == user_id).first()
    if streak is None:
        streak = UserStreak(
            user_id=user_id, current_streak=1, longest_streak=1, last_active_date=today
        )
        db.add(streak)
    else:
        if streak.last_active_date == today:
            pass  # already active today, no change
        elif streak.last_active_date == today - timedelta(days=1):
            streak.current_streak += 1
            streak.last_active_date = today
            if streak.current_streak > streak.longest_streak:
                streak.longest_streak = streak.current_streak
        else:
            streak.current_streak = 1
            streak.last_active_date = today
            if streak.current_streak > streak.longest_streak:
                streak.longest_streak = streak.current_streak


def check_milestones(db: Session, roadmap: Roadmap, before_pct: float, after_pct: float) -> None:
    """Fire in-app / email milestone notifications when progress crosses a threshold."""
    from app.notifications import notify_user

    for threshold in [25, 50, 75, 100]:
        if before_pct < threshold <= after_pct:
            message = f"You're {threshold}% done with {roadmap.title}"
            existing = (
                db.query(Notification)
                .filter(
                    Notification.user_id == roadmap.user_id,
                    Notification.roadmap_id == roadmap.id,
                    Notification.type == NotificationType.milestone,
                    Notification.message == message,
                )
                .first()
            )
            if not existing:
                notify_user(db, roadmap.user, NotificationType.milestone, message, roadmap.id)


# ---------------------------------------------------------------------------
# list_roadmaps
# ---------------------------------------------------------------------------


def list_roadmaps(db: Session, user_id: UUID) -> list[dict]:
    """Return a summary of every roadmap owned by *user_id*."""
    roadmaps = (
        db.query(Roadmap)
        .filter(Roadmap.user_id == user_id)
        .order_by(Roadmap.created_at.desc())
        .all()
    )
    return [
        {
            "id": str(rm.id),
            "title": rm.title,
            "type": rm.type.value,
            "status": rm.status.value,
            "progress_percentage": rm.progress_percentage,
        }
        for rm in roadmaps
    ]


# ---------------------------------------------------------------------------
# get_roadmap_progress
# ---------------------------------------------------------------------------


def get_roadmap_progress(db: Session, user_id: UUID, roadmap_id: UUID) -> dict | None:
    """Return progress stats for one roadmap, or None if not found / not owned."""
    roadmap = (
        db.query(Roadmap)
        .filter(Roadmap.id == roadmap_id, Roadmap.user_id == user_id)
        .first()
    )
    if roadmap is None:
        return None
    total = len(roadmap.nodes)
    completed = sum(1 for n in roadmap.nodes if n.completed)
    return {
        "roadmap_id": str(roadmap.id),
        "total_nodes": total,
        "completed_nodes": completed,
        "progress_percentage": roadmap.progress_percentage,
    }


# ---------------------------------------------------------------------------
# create_roadmap
# ---------------------------------------------------------------------------

TITLE_MAX = 255


def _derive_title(goal_text: str) -> str:
    title = " ".join(goal_text.strip().split())
    if len(title) > 80:
        title = title[:77].rstrip() + "..."
    return title[:TITLE_MAX]


def create_roadmap(
    db: Session,
    user_id: UUID,
    background_tasks: BackgroundTasks,
    goal_text: str,
    title: str | None = None,
    type=None,
) -> dict:
    """Create a Roadmap row committed as ``pending`` and enqueue background generation.

    Returns a dict with ``id``, ``title`` and ``status`` so the caller (route or chat tool)
    does not need to import Roadmap directly.
    """
    from app.generation import enqueue_generation
    from app.models import RoadmapType

    roadmap = Roadmap(
        user_id=user_id,
        title=title or _derive_title(goal_text),
        goal_text=goal_text,
        type=type if type is not None else RoadmapType.sequential,
        status=RoadmapStatus.pending,
    )
    db.add(roadmap)
    db.commit()
    db.refresh(roadmap)
    enqueue_generation(background_tasks, roadmap)
    return {
        "id": str(roadmap.id),
        "title": roadmap.title,
        "status": roadmap.status.value,
    }


# ---------------------------------------------------------------------------
# complete_node_by_name  (fuzzy match)
# ---------------------------------------------------------------------------


def complete_node_by_name(
    db: Session,
    user_id: UUID,
    roadmap_id: UUID,
    node_name: str,
) -> dict:
    """Complete a node matched by a fuzzy (case-insensitive substring) search.

    Returns a dict with one of these shapes:

    Success:
        {"completed": True, "node_id": "...", "node_name": "...", "message": "..."}

    Ambiguous / no match:
        {"completed": False, "clarification_needed": True,
         "message": "...", "candidates": [...]}
    """
    # Ownership check — 404 semantics become a "not found" result dict
    roadmap = (
        db.query(Roadmap)
        .filter(Roadmap.id == roadmap_id, Roadmap.user_id == user_id)
        .first()
    )
    if roadmap is None:
        return {
            "completed": False,
            "clarification_needed": False,
            "message": f"Roadmap {roadmap_id} not found.",
            "candidates": [],
        }

    needle = node_name.strip().lower()
    all_nodes = roadmap.nodes  # already loaded / ordered

    matches = [n for n in all_nodes if needle in n.name.lower()]

    if len(matches) == 0:
        candidates = [n.name for n in all_nodes]
        return {
            "completed": False,
            "clarification_needed": True,
            "message": (
                f"No node in this roadmap matches '{node_name}'. "
                f"Here are the available nodes: {candidates}"
            ),
            "candidates": candidates,
        }

    if len(matches) > 1:
        candidates = [n.name for n in matches]
        return {
            "completed": False,
            "clarification_needed": True,
            "message": (
                f"Multiple nodes match '{node_name}': {candidates}. "
                "Please specify which one you mean."
            ),
            "candidates": candidates,
        }

    # Exactly one match — complete it
    node = matches[0]
    before_pct = roadmap.progress_percentage
    node.completed = True
    update_streak(db, user_id)
    db.commit()
    db.refresh(roadmap)
    after_pct = roadmap.progress_percentage
    check_milestones(db, roadmap, before_pct, after_pct)
    db.commit()
    db.refresh(node)

    return {
        "completed": True,
        "node_id": str(node.id),
        "node_name": node.name,
        "message": f"Marked '{node.name}' as complete.",
    }
