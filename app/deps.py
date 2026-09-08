from uuid import UUID

from fastapi import Depends, HTTPException, Path, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Node, Roadmap, User
from app.security import get_current_user


def get_owned_roadmap(
    roadmap_id: UUID = Path(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Roadmap:
    """Resolve a roadmap scoped to the authenticated user.

    Another user's roadmap yields 404 rather than 403 so the API does not leak
    which roadmap ids exist.
    """
    roadmap = (
        db.query(Roadmap)
        .filter(Roadmap.id == roadmap_id, Roadmap.user_id == current_user.id)
        .first()
    )
    if roadmap is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Roadmap not found")
    return roadmap


def get_owned_node(
    node_id: UUID = Path(...),
    roadmap: Roadmap = Depends(get_owned_roadmap),
    db: Session = Depends(get_db),
) -> Node:
    node = (
        db.query(Node).filter(Node.id == node_id, Node.roadmap_id == roadmap.id).first()
    )
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    return node
