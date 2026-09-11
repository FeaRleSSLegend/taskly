from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from app.chat import run_chat
from app.database import get_db
from app.models import User
from app.schemas import ChatRequest, ChatResponse
from app.security import get_current_user

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
def chat(
    payload: ChatRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send a message to Nodi.

    Nodi can list your roadmaps, check progress, create new roadmaps, and mark
    nodes as complete — all via natural language.  No destructive operations
    (delete, edit, regenerate) are available through chat in this version.
    """
    return run_chat(db, current_user, payload.message, background_tasks)
