import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_owned_roadmap
from app.generation import enqueue_generation, reset_for_regeneration
from app.models import Roadmap, RoadmapStatus, User
from app.schemas import (
    GenerationStatus,
    Progress,
    RoadmapCreate,
    RoadmapDetail,
    RoadmapSummary,
    RoadmapUpdate,
    ValidationReport,
)
from app.security import get_current_user
from app.services import create_roadmap as svc_create_roadmap
from app.validation import validate_roadmap_graph

router = APIRouter(prefix="/roadmaps", tags=["roadmaps"])


@router.post("", response_model=RoadmapDetail, status_code=status.HTTP_201_CREATED)
def create_roadmap(
    payload: RoadmapCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = svc_create_roadmap(
        db,
        current_user.id,
        background_tasks,
        payload.goal_text,
        payload.title,
        payload.type,
    )
    # Re-fetch as a full ORM object so RoadmapDetail can serialise it.
    return db.query(Roadmap).filter(Roadmap.id == uuid.UUID(result["id"])).first()


@router.get("", response_model=list[RoadmapSummary])
def list_roadmaps(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    roadmaps = (
        db.query(Roadmap)
        .filter(Roadmap.user_id == current_user.id)
        .order_by(Roadmap.created_at.desc())
        .all()
    )
    return roadmaps


@router.get("/{roadmap_id}", response_model=RoadmapDetail)
def get_roadmap(roadmap: Roadmap = Depends(get_owned_roadmap)):
    return roadmap


@router.patch("/{roadmap_id}", response_model=RoadmapDetail)
def update_roadmap(
    payload: RoadmapUpdate,
    roadmap: Roadmap = Depends(get_owned_roadmap),
    db: Session = Depends(get_db),
):
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(roadmap, field, value)
    db.commit()
    db.refresh(roadmap)
    return roadmap


@router.delete("/{roadmap_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_roadmap(
    roadmap: Roadmap = Depends(get_owned_roadmap), db: Session = Depends(get_db)
):
    # Clear dependency edges first so the association rows go with the nodes on
    # every backend, then let the ORM cascade remove the nodes themselves.
    for node in roadmap.nodes:
        node.depends_on.clear()
        node.dependents.clear()
    db.flush()
    db.delete(roadmap)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{roadmap_id}/regenerate", response_model=RoadmapDetail)
def regenerate_roadmap(
    background_tasks: BackgroundTasks,
    roadmap: Roadmap = Depends(get_owned_roadmap),
    db: Session = Depends(get_db),
):
    reset_for_regeneration(db, roadmap)
    db.commit()
    db.refresh(roadmap)
    enqueue_generation(background_tasks, roadmap)
    return roadmap


@router.get("/{roadmap_id}/generation-status", response_model=GenerationStatus)
def generation_status(roadmap: Roadmap = Depends(get_owned_roadmap)):
    return GenerationStatus(
        roadmap_id=roadmap.id,
        status=roadmap.status,
        error_message=roadmap.error_message,
    )


@router.get("/{roadmap_id}/progress", response_model=Progress)
def roadmap_progress(
    roadmap: Roadmap = Depends(get_owned_roadmap),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    total = len(roadmap.nodes)
    completed = sum(1 for n in roadmap.nodes if n.completed)
    return Progress(
        roadmap_id=roadmap.id,
        total_nodes=total,
        completed_nodes=completed,
        progress_percentage=roadmap.progress_percentage,
    )


@router.post("/{roadmap_id}/validate", response_model=ValidationReport)
def validate_roadmap(
    roadmap: Roadmap = Depends(get_owned_roadmap), db: Session = Depends(get_db)
):
    problems = validate_roadmap_graph(db, roadmap)
    return ValidationReport(roadmap_id=roadmap.id, valid=not problems, problems=problems)
