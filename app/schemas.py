from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import NotificationType, RoadmapStatus, RoadmapType

# --- Auth ---------------------------------------------------------------


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def _fits_bcrypt(cls, value: str) -> str:
        # bcrypt hashes at most 72 *bytes*, so a max_length in characters is not
        # enough on its own once non-ASCII is in play.
        if len(value.encode("utf-8")) > 72:
            raise ValueError("password must be at most 72 bytes when UTF-8 encoded")
        return value


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class RegisterResponse(Token):
    user: UserOut


# --- Nodes --------------------------------------------------------------


class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    roadmap_id: UUID
    name: str
    phase: str | None = None
    description: str | None = None
    time_estimate: str | None = None
    depends_on: list[UUID] = []
    completed: bool
    order: int

    @field_validator("depends_on", mode="before")
    @classmethod
    def _flatten_dependencies(cls, value: Any) -> Any:
        # ORM gives us Node objects; the API contract is a flat list of ids.
        if isinstance(value, (list, tuple, set)):
            return [getattr(v, "id", v) for v in value]
        return value


class NodeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    phase: str | None = Field(default=None, max_length=255)
    description: str | None = None
    time_estimate: str | None = Field(default=None, max_length=100)
    depends_on: list[UUID] = []
    order: int | None = None


class NodeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    phase: str | None = Field(default=None, max_length=255)
    description: str | None = None
    time_estimate: str | None = Field(default=None, max_length=100)
    depends_on: list[UUID] | None = None
    order: int | None = None


class ReorderItem(BaseModel):
    node_id: UUID
    order: int


class ReorderRequest(BaseModel):
    items: list[ReorderItem] = Field(min_length=1)


# --- Roadmaps -----------------------------------------------------------


class RoadmapCreate(BaseModel):
    goal_text: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=255)
    type: RoadmapType = RoadmapType.sequential


class RoadmapUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    goal_text: str | None = Field(default=None, min_length=1)


class RoadmapSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    type: RoadmapType
    status: RoadmapStatus
    progress_percentage: float
    created_at: datetime


class RoadmapDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    title: str
    goal_text: str
    type: RoadmapType
    status: RoadmapStatus
    progress_percentage: float
    created_at: datetime
    error_message: str | None = None
    nodes: list[NodeOut] = []


class GenerationStatus(BaseModel):
    roadmap_id: UUID
    status: RoadmapStatus
    error_message: str | None = None


class Progress(BaseModel):
    roadmap_id: UUID
    total_nodes: int
    completed_nodes: int
    progress_percentage: float


# --- Validation ---------------------------------------------------------


class ValidationProblem(BaseModel):
    type: str  # "cycle" | "dangling_reference" | "foreign_reference" | "self_reference"
    message: str
    node_ids: list[UUID] = []


class ValidationReport(BaseModel):
    roadmap_id: UUID
    valid: bool
    problems: list[ValidationProblem] = []


# --- Streaks & Notifications -------------------------------------------


class UserStreakOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    current_streak: int
    longest_streak: int
    last_active_date: date | None = None


class NotificationPreferenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    email_enabled: bool
    milestone_notifications: bool


class NotificationPreferenceUpdate(BaseModel):
    email_enabled: bool | None = None
    milestone_notifications: bool | None = None


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    type: NotificationType
    message: str
    roadmap_id: UUID | None = None
    delivered: bool
    created_at: datetime


# --- Chat (Nodi) --------------------------------------------------------


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)


class ActionTaken(BaseModel):
    """Summary of one tool call, returned to the frontend so it can render
    something like '✓ Created roadmap: Learn Rust' alongside the chat bubble."""

    tool: str
    result: str  # natural-language one-liner


class ChatResponse(BaseModel):
    reply: str
    actions_taken: list[ActionTaken] = []
