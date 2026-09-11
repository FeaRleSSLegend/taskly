"""Nodi — AI chat orchestration using Groq tool calling.

This module is entirely separate from app/generation.py:
- generation.py  → structured outputs (response_format JSON schema)
- chat.py        → tool calling  (tools= parameter, finish_reason="tool_calls")

The four tools each delegate to a service function in app/services.py, which is
the same code path the REST endpoints use.  No roadmap/node logic is
reimplemented here.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ChatMessage, ChatRole, User
from app.schemas import ActionTaken, ChatResponse
from app.services import (
    complete_node_by_name,
    create_roadmap,
    get_roadmap_progress,
    list_roadmaps,
)

logger = logging.getLogger(__name__)

# Maximum Groq tool-call iterations before we stop looping and return whatever
# partial reply we have (prevents runaway loops on a misbehaving model).
MAX_TOOL_ITERATIONS = 4

# How many recent ChatMessages to send as context.
HISTORY_LIMIT = 20

# ---------------------------------------------------------------------------
# Groq client
# ---------------------------------------------------------------------------


def build_chat_client():
    """Construct a Groq client for tool-calling, or None when no key is set.

    Mirrors generation.build_client() so tests can monkeypatch this
    independently without touching the generation machinery.
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


# ---------------------------------------------------------------------------
# Tool definitions sent to Groq
# ---------------------------------------------------------------------------

CHAT_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_roadmaps",
            "description": (
                "List all roadmaps that belong to the current user. "
                "Returns id, title, type, status, and progress_percentage for each."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_roadmap_progress",
            "description": (
                "Get detailed progress for one roadmap: total nodes, "
                "completed nodes, and progress percentage."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "roadmap_id": {
                        "type": "string",
                        "description": "UUID of the roadmap to check.",
                    }
                },
                "required": ["roadmap_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_roadmap",
            "description": (
                "Create a new learning roadmap for the user. "
                "The roadmap is created immediately as 'pending'; AI generation "
                "runs in the background. Returns the roadmap id and title."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_text": {
                        "type": "string",
                        "description": (
                            "A description of what the user wants to learn or achieve. "
                            "Use the user's own words where possible."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "Optional short title for the roadmap. "
                            "If omitted the title is derived from goal_text."
                        ),
                    },
                },
                "required": ["goal_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_node",
            "description": (
                "Mark a node in a roadmap as complete. "
                "The node is identified by a fuzzy text match on its name "
                "(e.g. 'mark ownership as done'). "
                "If the match is ambiguous the tool returns a list of candidates "
                "instead of guessing — in that case ask the user to clarify."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "roadmap_id": {
                        "type": "string",
                        "description": "UUID of the roadmap containing the node.",
                    },
                    "node_name": {
                        "type": "string",
                        "description": "The name (or partial name) of the node to complete.",
                    },
                },
                "required": ["roadmap_id", "node_name"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------


def _dispatch_tool(
    db: Session,
    user: User,
    background_tasks: BackgroundTasks,
    tool_name: str,
    tool_args: dict[str, Any],
) -> tuple[str, str]:
    """Execute *tool_name* and return (json_result, human_summary).

    The json_result is fed back to Groq as a tool-role message.
    The human_summary is collected for the ChatResponse.actions_taken list.
    """
    if tool_name == "list_roadmaps":
        result = list_roadmaps(db, user.id)
        summary = f"Listed {len(result)} roadmap(s)."
        return json.dumps(result), summary

    if tool_name == "get_roadmap_progress":
        roadmap_id_str = tool_args.get("roadmap_id", "")
        try:
            roadmap_id = UUID(roadmap_id_str)
        except ValueError:
            result = {"error": f"Invalid roadmap_id: {roadmap_id_str!r}"}
            return json.dumps(result), "Invalid roadmap ID."
        result = get_roadmap_progress(db, user.id, roadmap_id)
        if result is None:
            result = {"error": "Roadmap not found."}
            return json.dumps(result), "Roadmap not found."
        summary = (
            f"Progress for roadmap {roadmap_id_str}: "
            f"{result['completed_nodes']}/{result['total_nodes']} nodes "
            f"({result['progress_percentage']}%)."
        )
        return json.dumps(result), summary

    if tool_name == "create_roadmap":
        goal_text = tool_args.get("goal_text", "")
        title = tool_args.get("title")
        result = create_roadmap(db, user.id, background_tasks, goal_text, title)
        summary = f"Created roadmap '{result['title']}' (id: {result['id']}, status: {result['status']})."
        return json.dumps(result), summary

    if tool_name == "complete_node":
        roadmap_id_str = tool_args.get("roadmap_id", "")
        node_name = tool_args.get("node_name", "")
        try:
            roadmap_id = UUID(roadmap_id_str)
        except ValueError:
            result = {"error": f"Invalid roadmap_id: {roadmap_id_str!r}"}
            return json.dumps(result), "Invalid roadmap ID."
        result = complete_node_by_name(db, user.id, roadmap_id, node_name)
        if result.get("completed"):
            summary = f"Completed node '{result['node_name']}'."
        else:
            summary = result.get("message", "Could not complete node.")
        return json.dumps(result), summary

    # Unknown tool — tell the model gracefully
    result = {"error": f"Unknown tool: {tool_name!r}"}
    return json.dumps(result), f"Unknown tool '{tool_name}' was called."


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are Nodi, a warm and encouraging AI assistant built into a roadmap app. "
    "Your job is to help users turn their learning goals into structured roadmaps "
    "and to track their progress. You have access to tools to list their roadmaps, "
    "check progress, create new roadmaps, and mark nodes as complete. "
    "Keep replies concise and friendly — match the tone of messages like "
    "'your roadmap is ready' and 'you\u2019re 50% done'. "
    "When you create a roadmap, reassure the user that AI generation is running "
    "in the background and they can check back shortly. "
    "You are not a general-purpose chatbot; gently redirect off-topic questions "
    "back to roadmaps and goals."
)

# ---------------------------------------------------------------------------
# Main chat entry point
# ---------------------------------------------------------------------------


def run_chat(
    db: Session,
    user: User,
    message: str,
    background_tasks: BackgroundTasks,
) -> ChatResponse:
    """Orchestrate one Nodi chat turn.

    1. Load the last HISTORY_LIMIT ChatMessages for the user (context).
    2. Append + save the new user message.
    3. Call Groq with tool definitions.
    4. Loop up to MAX_TOOL_ITERATIONS: execute any tool calls, feed results back.
    5. Save the assistant's final reply and return ChatResponse.
    """
    client = build_chat_client()
    if client is None:
        # No API key configured — return a graceful degraded reply without 500ing.
        _save_message(db, user.id, ChatRole.user, message)
        reply = (
            "I'm not fully set up yet — the AI backend isn't configured. "
            "Please ask your admin to set GROQ_API_KEY."
        )
        _save_message(db, user.id, ChatRole.assistant, reply)
        return ChatResponse(reply=reply)

    # --- 1. Load history ---------------------------------------------------
    history_rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == user.id)
        .order_by(ChatMessage.created_at.asc())
        .limit(HISTORY_LIMIT)
        .all()
    )
    messages: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT}]
    for row in history_rows:
        messages.append({"role": row.role.value, "content": row.content})

    # --- 2. Append the new user message -----------------------------------
    _save_message(db, user.id, ChatRole.user, message)
    messages.append({"role": "user", "content": message})

    # --- 3 & 4. Tool-call loop --------------------------------------------
    settings = get_settings()
    actions_taken: list[ActionTaken] = []
    final_reply: str = ""

    for _iteration in range(MAX_TOOL_ITERATIONS + 1):
        try:
            response = client.chat.completions.create(
                model=settings.groq_model,
                messages=messages,
                tools=CHAT_TOOLS,
                tool_choice="auto",
                temperature=0.5,
            )
        except Exception as exc:
            logger.exception("Groq chat call failed: %s", exc)
            final_reply = (
                "I ran into a problem talking to the AI. Please try again in a moment."
            )
            break

        choice = response.choices[0]
        finish_reason = choice.finish_reason

        if finish_reason == "tool_calls":
            # Append the assistant's tool-call message to history
            assistant_msg = choice.message
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in (assistant_msg.tool_calls or [])
                    ],
                }
            )

            # Execute each tool call and collect results
            for tool_call in assistant_msg.tool_calls or []:
                tool_name = tool_call.function.name
                try:
                    tool_args = json.loads(tool_call.function.arguments or "{}")
                except (TypeError, ValueError):
                    tool_args = {}

                json_result, summary = _dispatch_tool(
                    db, user, background_tasks, tool_name, tool_args
                )
                actions_taken.append(ActionTaken(tool=tool_name, result=summary))

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json_result,
                    }
                )

            # If we've hit the cap, stop looping even though finish_reason was
            # "tool_calls" — use whatever partial reply the model gave (usually
            # empty) and break out.
            if _iteration >= MAX_TOOL_ITERATIONS:
                final_reply = (
                    assistant_msg.content
                    or "I've taken several actions — let me know if you'd like more details."
                )
                break

        else:
            # "stop" or any other terminal reason — we have our final reply
            final_reply = choice.message.content or ""
            break

    # --- 5. Persist assistant reply ---------------------------------------
    _save_message(db, user.id, ChatRole.assistant, final_reply)

    return ChatResponse(reply=final_reply, actions_taken=actions_taken)


def _save_message(db: Session, user_id: UUID, role: ChatRole, content: str) -> ChatMessage:
    """Persist a single ChatMessage row and commit so the message is saved."""
    msg = ChatMessage(user_id=user_id, role=role, content=content)
    db.add(msg)
    db.commit()
    return msg
