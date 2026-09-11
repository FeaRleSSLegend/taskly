import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.database import Base


class RoadmapType(str, enum.Enum):
    sequential = "sequential"
    flat = "flat"


class RoadmapStatus(str, enum.Enum):
    pending = "pending"
    generating_phases = "generating_phases"
    generating_tasks = "generating_tasks"
    done = "done"
    failed = "failed"


class NotificationType(str, enum.Enum):
    roadmap_ready = "roadmap_ready"
    milestone = "milestone"


class ChatRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"
    tool = "tool"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enum_col(py_enum, name):
    # Store the lowercase *values* rather than the python member names.
    return Enum(py_enum, name=name, values_callable=lambda e: [m.value for m in e])


# Self-referential association table for node dependencies.
# A row (depends_on_node_id=A, dependent_node_id=B) means "B depends on A".
node_dependencies = Table(
    "node_dependencies",
    Base.metadata,
    Column(
        "depends_on_node_id",
        Uuid,
        ForeignKey("nodes.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "dependent_node_id",
        Uuid,
        ForeignKey("nodes.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    UniqueConstraint("depends_on_node_id", "dependent_node_id", name="uq_node_dependency"),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    roadmaps: Mapped[list["Roadmap"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    streak: Mapped["UserStreak | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    preference: Mapped["NotificationPreference | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    chat_messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Roadmap(Base):
    __tablename__ = "roadmaps"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    goal_text: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[RoadmapType] = mapped_column(
        _enum_col(RoadmapType, "roadmap_type"), nullable=False, default=RoadmapType.sequential
    )
    status: Mapped[RoadmapStatus] = mapped_column(
        _enum_col(RoadmapStatus, "roadmap_status"), nullable=False, default=RoadmapStatus.pending
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    # Why generation failed, when status is `failed`. Cleared on a new attempt.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="roadmaps")
    nodes: Mapped[list["Node"]] = relationship(
        back_populates="roadmap",
        cascade="all, delete-orphan",
        order_by="Node.order",
    )

    @property
    def progress_percentage(self) -> float:
        if not self.nodes:
            return 0.0
        done = sum(1 for n in self.nodes if n.completed)
        return round(done / len(self.nodes) * 100, 2)


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    roadmap_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("roadmaps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phase: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    time_estimate: Mapped[str | None] = mapped_column(String(100), nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    roadmap: Mapped["Roadmap"] = relationship(back_populates="nodes")

    # Nodes this node depends on (its prerequisites).
    depends_on: Mapped[list["Node"]] = relationship(
        secondary=node_dependencies,
        primaryjoin=lambda: Node.id == node_dependencies.c.dependent_node_id,
        secondaryjoin=lambda: Node.id == node_dependencies.c.depends_on_node_id,
        back_populates="dependents",
    )
    # Nodes that depend on this node.
    dependents: Mapped[list["Node"]] = relationship(
        secondary=node_dependencies,
        primaryjoin=lambda: Node.id == node_dependencies.c.depends_on_node_id,
        secondaryjoin=lambda: Node.id == node_dependencies.c.dependent_node_id,
        back_populates="depends_on",
    )


class UserStreak(Base):
    __tablename__ = "user_streaks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    current_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    longest_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_active_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    user: Mapped["User"] = relationship(back_populates="streak")


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    email_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    milestone_notifications: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    user: Mapped["User"] = relationship(back_populates="preference")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[NotificationType] = mapped_column(
        _enum_col(NotificationType, "notification_type"), nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    roadmap_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("roadmaps.id", ondelete="SET NULL"), nullable=True
    )
    delivered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="notifications")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[ChatRole] = mapped_column(
        _enum_col(ChatRole, "chat_role"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="chat_messages")
