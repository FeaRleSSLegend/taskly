"""Roadmap generation backed by Groq structured outputs.

Flow, run in a FastAPI BackgroundTask so POST /roadmaps returns immediately:

  1. classify    -> sequential | flat            (status: generating_phases)
  2a. flat       -> one call, independent tasks  (status: done)
  2b. sequential -> phases, then one call per
      phase for its atomic tasks                 (status: generating_tasks)

Every call uses ``response_format={"type": "json_schema", ..., "strict": True}``,
so the model can only return schema-conforming JSON and there is no free-text
parsing, repair, or retry loop to maintain.

Cross-phase dependencies are expressed as *indices* into the previous phase's
task list rather than as ids the model invents, so a hallucinated reference is
an out-of-range integer we can drop rather than a dangling edge. Combined with
within-phase dependencies being restricted to strictly earlier indices, the
generated graph is acyclic by construction; ``validate_roadmap_graph`` still
runs before the roadmap is marked ``done`` as a backstop.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app import database
from app.config import get_settings
from app.models import Node, Roadmap, RoadmapStatus, RoadmapType

logger = logging.getLogger(__name__)

MIN_PHASES = 4
MAX_PHASES = 8


# --- JSON schemas for structured outputs ---------------------------------


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    """Strict mode requires every property to be required and no extras."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


CLASSIFY_SCHEMA = _obj(
    {
        "type": {
            "type": "string",
            "enum": ["sequential", "flat"],
            "description": (
                "'sequential' if the goal has genuine ordering constraints where "
                "later work depends on earlier work; 'flat' if the tasks are "
                "independent and can be done in any order."
            ),
        },
        "reasoning": {
            "type": "string",
            "description": "One sentence justifying the choice.",
        },
    }
)

_TASK_PROPERTIES = {
    "name": {"type": "string", "description": "Short imperative task name."},
    "description": {
        "type": "string",
        "description": "One or two sentences on what doing this task involves.",
    },
    "time_estimate": {
        "type": "string",
        "description": "Human-readable estimate, e.g. '30 minutes', '2 hours', '3 days'.",
    },
}

FLAT_TASKS_SCHEMA = _obj(
    {
        "tasks": {
            "type": "array",
            "minItems": 3,
            "maxItems": 25,
            "items": _obj(dict(_TASK_PROPERTIES)),
        }
    }
)

PHASES_SCHEMA = _obj(
    {
        "phases": {
            "type": "array",
            "minItems": MIN_PHASES,
            "maxItems": MAX_PHASES,
            "items": _obj(
                {
                    "name": {"type": "string", "description": "Short phase name."},
                    "description": {
                        "type": "string",
                        "description": "One line on what this phase covers.",
                    },
                }
            ),
        }
    }
)

PHASE_TASKS_SCHEMA = _obj(
    {
        "tasks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": _obj(
                {
                    **_TASK_PROPERTIES,
                    "depends_on_previous_phase": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "Zero-based indices of tasks in the PREVIOUS phase that "
                            "must be finished before this task can start. Empty list "
                            "if this task does not depend on the previous phase."
                        ),
                    },
                    "depends_on_earlier_in_phase": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "Zero-based indices of EARLIER tasks in this same phase "
                            "that this task depends on. Each must be smaller than "
                            "this task's own index. Empty list if there are none."
                        ),
                    },
                }
            ),
        }
    }
)


# --- Groq plumbing -------------------------------------------------------


class GenerationError(RuntimeError):
    """Any failure that should park the roadmap in `failed`."""


def build_client():
    """Construct a Groq client, or return None when no API key is configured.

    Tests monkeypatch this to hand back a fake client, which is how the suite
    covers the whole flow without spending real API calls.
    """
    settings = get_settings()
    if not settings.groq_api_key:
        return None

    from groq import Groq

    return Groq(
        api_key=settings.groq_api_key,
        timeout=settings.groq_timeout_seconds,
        max_retries=settings.groq_max_retries,
    )


def _call(client, *, name: str, schema: dict, system: str, user: str) -> dict:
    """One structured-output completion. Raises GenerationError on any failure."""
    settings = get_settings()
    try:
        completion = client.chat.completions.create(
            model=settings.groq_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.4,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": name, "schema": schema, "strict": True},
            },
        )
        content = completion.choices[0].message.content
    except Exception as exc:  # groq API errors, timeouts, malformed responses
        raise GenerationError(f"Groq call '{name}' failed: {exc}") from exc

    try:
        parsed = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise GenerationError(f"Groq call '{name}' returned unparseable JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise GenerationError(
            f"Groq call '{name}' returned {type(parsed).__name__}, expected an object"
        )
    return parsed


# --- The individual generation steps -------------------------------------

_CLASSIFY_SYSTEM = (
    "You classify a personal goal by its structure. Answer 'sequential' when the "
    "goal has genuine ordering constraints - learning a skill, a multi-stage "
    "project, anything where later work is only possible once earlier work is "
    "done. Answer 'flat' when the items are independent and could be tackled in "
    "any order, such as an errand list or a shopping list."
)

_PLANNER_SYSTEM = (
    "You are a planner that decomposes goals into concrete, actionable tasks. "
    "Every task must be something a person can sit down and do. Avoid vague "
    "tasks like 'research' or 'get started'; say what specifically to do. Give "
    "each task a realistic time estimate."
)


def classify_goal(client, goal_text: str) -> RoadmapType:
    result = _call(
        client,
        name="goal_classification",
        schema=CLASSIFY_SCHEMA,
        system=_CLASSIFY_SYSTEM,
        user=f"Classify this goal:\n\n{goal_text}",
    )
    raw = result.get("type")
    try:
        return RoadmapType(raw)
    except ValueError as exc:
        raise GenerationError(f"Classifier returned unknown type {raw!r}") from exc


def generate_flat_tasks(client, goal_text: str) -> list[dict]:
    result = _call(
        client,
        name="flat_tasks",
        schema=FLAT_TASKS_SCHEMA,
        system=_PLANNER_SYSTEM,
        user=(
            "Break this goal into a flat checklist of independent tasks. The tasks "
            "have no ordering constraints between them, so do not imply any.\n\n"
            f"Goal: {goal_text}"
        ),
    )
    tasks = result.get("tasks") or []
    if not tasks:
        raise GenerationError("Flat generation returned no tasks")
    return tasks


def generate_phases(client, goal_text: str) -> list[dict]:
    result = _call(
        client,
        name="roadmap_phases",
        schema=PHASES_SCHEMA,
        system=_PLANNER_SYSTEM,
        user=(
            f"Break this goal into {MIN_PHASES}-{MAX_PHASES} major phases, in the "
            "order they must happen. Give each a name and a one-line description. "
            "Do not list individual tasks yet.\n\n"
            f"Goal: {goal_text}"
        ),
    )
    phases = result.get("phases") or []
    if not phases:
        raise GenerationError("Phase generation returned no phases")
    return phases


def generate_phase_tasks(
    client,
    goal_text: str,
    phases: list[dict],
    index: int,
    previous_tasks: list[dict],
) -> list[dict]:
    """Tasks for one phase, given the adjacent phases as context."""
    phase = phases[index]
    previous_phase = phases[index - 1] if index > 0 else None
    next_phase = phases[index + 1] if index + 1 < len(phases) else None

    lines = [f"Overall goal: {goal_text}", "", f"Full phase list ({len(phases)} phases):"]
    for i, item in enumerate(phases):
        marker = "  <-- the phase you are working on" if i == index else ""
        lines.append(f"  {i + 1}. {item.get('name')}: {item.get('description')}{marker}")

    if previous_phase is None:
        lines += ["", "This is the FIRST phase, so no task may depend on a previous phase."]
    else:
        lines += ["", f"Tasks in the previous phase ('{previous_phase.get('name')}'):"]
        for i, task in enumerate(previous_tasks):
            lines.append(f"  [{i}] {task.get('name')}")
        lines.append(
            "Use these indices in depends_on_previous_phase to say which of them a "
            "task actually needs. Only link a task where there is a real dependency."
        )

    if next_phase is not None:
        lines += [
            "",
            f"The phase after this one is '{next_phase.get('name')}'. Leave room for "
            "it; do not do its work here.",
        ]

    result = _call(
        client,
        name="phase_tasks",
        schema=PHASE_TASKS_SCHEMA,
        system=_PLANNER_SYSTEM,
        user=(
            "\n".join(lines)
            + f"\n\nList the atomic tasks for phase '{phase.get('name')}'."
        ),
    )
    tasks = result.get("tasks") or []
    if not tasks:
        raise GenerationError(f"Phase '{phase.get('name')}' returned no tasks")
    return tasks


# --- Persisting the result ----------------------------------------------


def _make_node(roadmap_id: UUID, task: dict, order: int, phase: str | None = None) -> Node:
    estimate = task.get("time_estimate")
    return Node(
        roadmap_id=roadmap_id,
        name=str(task.get("name") or "Untitled task")[:255],
        phase=str(phase)[:255] if phase else None,
        description=task.get("description"),
        time_estimate=str(estimate)[:100] if estimate else None,
        order=order,
    )


def _persist_flat(db: Session, roadmap: Roadmap, tasks: list[dict]) -> None:
    for order, task in enumerate(tasks):
        db.add(_make_node(roadmap.id, task, order))
    db.flush()


def _persist_sequential(
    db: Session, roadmap: Roadmap, phases: list[dict], phase_tasks: list[list[dict]]
) -> None:
    """Create every node first, then wire the dependency edges between them."""
    created: list[list[Node]] = []
    order = 0
    for phase_index, tasks in enumerate(phase_tasks):
        phase_name = phases[phase_index].get("name") if phase_index < len(phases) else None
        row: list[Node] = []
        for task in tasks:
            node = _make_node(roadmap.id, task, order, phase=phase_name)
            db.add(node)
            row.append(node)
            order += 1
        created.append(row)
    db.flush()

    for phase_index, tasks in enumerate(phase_tasks):
        previous = created[phase_index - 1] if phase_index > 0 else []
        for task_index, task in enumerate(tasks):
            node = created[phase_index][task_index]
            prerequisites: list[Node] = []

            for i in _as_indices(task.get("depends_on_previous_phase")):
                if 0 <= i < len(previous):
                    prerequisites.append(previous[i])
                else:
                    logger.warning(
                        "Dropping out-of-range cross-phase dependency %s on node %r",
                        i,
                        node.name,
                    )

            for i in _as_indices(task.get("depends_on_earlier_in_phase")):
                # Strictly earlier only; this is what makes a cycle impossible.
                if 0 <= i < task_index:
                    prerequisites.append(created[phase_index][i])
                else:
                    logger.warning(
                        "Dropping non-backward within-phase dependency %s on node %r",
                        i,
                        node.name,
                    )

            # A task in phase N+1 with no stated link would otherwise float free
            # of the ordering, so fall back to the previous phase's last task.
            if not prerequisites and previous:
                prerequisites.append(previous[-1])

            node.depends_on = list(dict.fromkeys(prerequisites))
    db.flush()


def _as_indices(value: Any) -> list[int]:
    """Coerce a model-supplied dependency list into plain ints, dropping junk."""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            out.append(item)
        elif isinstance(item, str) and item.strip().lstrip("-").isdigit():
            out.append(int(item))
    return out


def _clear_nodes(db: Session, roadmap: Roadmap) -> None:
    """Delete a roadmap's nodes through the ORM so the node_dependencies rows go
    with them on every backend."""
    for node in db.query(Node).filter(Node.roadmap_id == roadmap.id).all():
        node.depends_on.clear()
        node.dependents.clear()
        db.delete(node)
    db.flush()
    db.expire(roadmap, ["nodes"])


# --- Orchestration -------------------------------------------------------


def run_generation(roadmap_id: UUID) -> None:
    """Entry point for the background task. Owns its own session, because the
    request-scoped one is already closed by the time this runs.

    Never raises: any failure is recorded on the roadmap as `failed` plus an
    error message, so the background task cannot die silently.
    """
    from app.validation import validate_roadmap_graph

    try:
        client = build_client()
    except Exception as exc:
        logger.exception("Could not build the Groq client for roadmap %s", roadmap_id)
        _record_failure(roadmap_id, exc)
        return

    if client is None:
        logger.warning(
            "GROQ_API_KEY is not set; roadmap %s stays in 'pending' with no nodes",
            roadmap_id,
        )
        return

    try:
        with database.session_scope() as db:
            roadmap = db.get(Roadmap, roadmap_id)
            if roadmap is None:
                logger.warning("Roadmap %s vanished before generation started", roadmap_id)
                return

            goal_text = roadmap.goal_text
            roadmap.error_message = None
            roadmap.status = RoadmapStatus.generating_phases
            db.flush()

            roadmap_type = classify_goal(client, goal_text)
            roadmap.type = roadmap_type
            db.flush()

            if roadmap_type is RoadmapType.flat:
                tasks = generate_flat_tasks(client, goal_text)
                _clear_nodes(db, roadmap)
                _persist_flat(db, roadmap, tasks)
            else:
                phases = generate_phases(client, goal_text)
                roadmap.status = RoadmapStatus.generating_tasks
                db.flush()

                phase_tasks: list[list[dict]] = []
                previous: list[dict] = []
                for index in range(len(phases)):
                    tasks = generate_phase_tasks(client, goal_text, phases, index, previous)
                    phase_tasks.append(tasks)
                    previous = tasks

                _clear_nodes(db, roadmap)
                _persist_sequential(db, roadmap, phases, phase_tasks)

            problems = validate_roadmap_graph(db, roadmap)
            if problems:
                # Should be unreachable given how dependencies are built above,
                # but a broken graph is worse than no graph.
                raise GenerationError(
                    "Generated graph failed validation: "
                    + "; ".join(p.message for p in problems)
                )

            roadmap.status = RoadmapStatus.done
            logger.info("Generated roadmap %s (%s)", roadmap_id, roadmap_type.value)

    except Exception as exc:
        logger.exception("Generation failed for roadmap %s", roadmap_id)
        _record_failure(roadmap_id, exc)


def _record_failure(roadmap_id: UUID, exc: BaseException) -> None:
    """Mark the roadmap failed in a fresh session, since the generation session
    was rolled back along with any partially built graph."""
    try:
        with database.session_scope() as db:
            roadmap = db.get(Roadmap, roadmap_id)
            if roadmap is None:
                return
            _clear_nodes(db, roadmap)
            roadmap.status = RoadmapStatus.failed
            roadmap.error_message = str(exc)[:2000] or exc.__class__.__name__
    except Exception:
        logger.exception("Could not record generation failure for roadmap %s", roadmap_id)


def enqueue_generation(background_tasks, roadmap: Roadmap) -> None:
    """Schedule generation for a roadmap that is already committed as `pending`."""
    background_tasks.add_task(run_generation, roadmap.id)


def reset_for_regeneration(db: Session, roadmap: Roadmap) -> None:
    """Clear the existing graph and put the roadmap back in `pending`, ready for
    `enqueue_generation` to re-run decomposition on the same goal text."""
    _clear_nodes(db, roadmap)
    roadmap.status = RoadmapStatus.pending
    roadmap.error_message = None
