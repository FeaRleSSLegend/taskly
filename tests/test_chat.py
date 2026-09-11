"""Tests for the Nodi AI chat feature (POST /chat).

Groq is never called for real: app.chat.build_chat_client is monkeypatched to
return a FakeGroqChat whose chat.completions.create method returns canned
responses that exercise the desired tool-call path.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.models import ChatMessage, Node, Roadmap, UserStreak
from tests.conftest import make_node, register


# ---------------------------------------------------------------------------
# Helpers to build fake Groq completion objects
# ---------------------------------------------------------------------------


def _make_tool_call(name: str, args: dict, call_id: str | None = None) -> SimpleNamespace:
    """Build a fake tool-call object matching the Groq SDK's shape."""
    return SimpleNamespace(
        id=call_id or f"call_{name}_{uuid.uuid4().hex[:6]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _tool_response(tool_calls: list) -> SimpleNamespace:
    """A fake Groq response whose finish_reason is 'tool_calls'."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=None,
                    tool_calls=tool_calls,
                ),
            )
        ]
    )


def _text_response(text: str) -> SimpleNamespace:
    """A fake Groq response with a plain text reply (finish_reason='stop')."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=text, tool_calls=None),
            )
        ]
    )


def _make_fake_client(*responses) -> MagicMock:
    """Return a mock Groq client whose completions.create returns *responses in order."""
    client = MagicMock()
    client.chat.completions.create.side_effect = list(responses)
    return client


# ---------------------------------------------------------------------------
# db_session fixture (same pattern as test_streaks_and_notifications)
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(db_engine):
    TestingSession = sessionmaker(bind=db_engine, autoflush=False, autocommit=False, future=True)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Test 1 — pure question: list_roadmaps, no rows created
# ---------------------------------------------------------------------------


def test_list_roadmaps_pure_question(client, auth, monkeypatch):
    """A 'show me my roadmaps' message should call list_roadmaps and return a
    friendly reply, without creating any new Roadmap rows."""
    roadmap_resp = client.post(
        "/roadmaps",
        json={"goal_text": "Learn Python", "type": "flat"},
        headers=auth,
    )
    assert roadmap_resp.status_code == 201
    rm_id = roadmap_resp.json()["id"]

    tc = _make_tool_call("list_roadmaps", {})
    fake_client = _make_fake_client(
        _tool_response([tc]),
        _text_response("Here are your roadmaps: you have 1 roadmap — 'Learn Python'."),
    )
    monkeypatch.setattr("app.chat.build_chat_client", lambda: fake_client)

    # count roadmaps before
    resp_before = client.get("/roadmaps", headers=auth).json()

    chat_resp = client.post("/chat", json={"message": "Show me my roadmaps"}, headers=auth)
    assert chat_resp.status_code == 200
    body = chat_resp.json()
    assert "roadmap" in body["reply"].lower()
    assert len(body["actions_taken"]) == 1
    assert body["actions_taken"][0]["tool"] == "list_roadmaps"

    # No new roadmap should have been created
    resp_after = client.get("/roadmaps", headers=auth).json()
    assert len(resp_after) == len(resp_before)


# ---------------------------------------------------------------------------
# Test 2 — create_roadmap results in a real Roadmap row via shared service
# ---------------------------------------------------------------------------


def test_create_roadmap_creates_real_row(client, auth, monkeypatch, db_session):
    """A create-roadmap request must create a real Roadmap row through the same
    service function POST /roadmaps uses — not a separate chat-only code path."""
    goal = "Learn Rust programming language"

    # We need to capture what roadmap id the tool call produces so we can
    # check it in the DB.  We do this by letting the real service run and
    # just mocking build_chat_client to steer tool calls.
    created_id_holder: list[str] = []

    def fake_create_side_effect(*args, **kwargs):
        messages = args[0] if args else kwargs.get("messages", [])
        # First call → ask to create a roadmap
        if fake_create_side_effect.call_count == 1:
            fake_create_side_effect.call_count += 1
            tc = _make_tool_call("create_roadmap", {"goal_text": goal}, "call_cr_1")
            return _tool_response([tc])
        else:
            # Parse out the roadmap id from the tool result in the messages
            for m in reversed(messages):
                if m.get("role") == "tool":
                    try:
                        data = json.loads(m["content"])
                        if "id" in data:
                            created_id_holder.append(data["id"])
                    except Exception:
                        pass
            return _text_response(
                f"Done! I've created a roadmap for '{goal}', it's generating now."
            )

    fake_create_side_effect.call_count = 1

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = fake_create_side_effect
    monkeypatch.setattr("app.chat.build_chat_client", lambda: fake_client)

    chat_resp = client.post(
        "/chat", json={"message": f"Create a roadmap for {goal}"}, headers=auth
    )
    assert chat_resp.status_code == 200
    body = chat_resp.json()
    assert "generating" in body["reply"].lower() or "created" in body["reply"].lower()
    assert any(a["tool"] == "create_roadmap" for a in body["actions_taken"])

    # Verify the DB actually has the row
    assert created_id_holder, "Tool result did not return an id"
    roadmap_id = uuid.UUID(created_id_holder[0])
    rm = db_session.get(Roadmap, roadmap_id)
    assert rm is not None
    assert rm.goal_text == goal


# ---------------------------------------------------------------------------
# Helper: decode a test auth token to get the user_id
# ---------------------------------------------------------------------------


def _user_id_from_auth(auth: dict) -> uuid.UUID:
    from jose import jwt as jose_jwt
    from app.config import get_settings

    token = auth["Authorization"].split(" ", 1)[1]
    settings = get_settings()
    payload = jose_jwt.decode(
        token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
    )
    return uuid.UUID(payload["sub"])


# ---------------------------------------------------------------------------
# Test 3 — complete_node with ambiguous name returns clarification
# ---------------------------------------------------------------------------


def test_complete_node_ambiguous_name(client, auth, roadmap, monkeypatch, db_session):
    """When two nodes match the fuzzy search the tool should return a
    clarification result.  Neither node should be marked complete."""
    n1 = make_node(client, auth, roadmap["id"], "Ownership model basics").json()
    n2 = make_node(client, auth, roadmap["id"], "Advanced ownership patterns").json()

    rm_id = roadmap["id"]

    tc = _make_tool_call(
        "complete_node",
        {"roadmap_id": rm_id, "node_name": "ownership"},
        "call_cn_1",
    )

    fake_client = _make_fake_client(
        _tool_response([tc]),
        _text_response(
            "There are two nodes matching 'ownership'. "
            "Did you mean 'Ownership model basics' or 'Advanced ownership patterns'?"
        ),
    )
    monkeypatch.setattr("app.chat.build_chat_client", lambda: fake_client)

    chat_resp = client.post(
        "/chat", json={"message": "Mark ownership as done"}, headers=auth
    )
    assert chat_resp.status_code == 200
    body = chat_resp.json()
    # The reply should mention both candidates
    assert "ownership" in body["reply"].lower()
    # The action result should indicate clarification needed
    cn_action = next(a for a in body["actions_taken"] if a["tool"] == "complete_node")
    assert "ambiguous" in cn_action["result"].lower() or "multiple" in cn_action["result"].lower() or "match" in cn_action["result"].lower()

    # Neither node should be completed
    db_session.expire_all()
    db_n1 = db_session.get(Node, uuid.UUID(n1["id"]))
    db_n2 = db_session.get(Node, uuid.UUID(n2["id"]))
    assert db_n1 is not None and not db_n1.completed
    assert db_n2 is not None and not db_n2.completed


# ---------------------------------------------------------------------------
# Test 4 — complete_node with clear match completes the node + side effects
# ---------------------------------------------------------------------------


def test_complete_node_clear_match_triggers_streak_and_milestones(
    client, auth, roadmap, monkeypatch, db_session
):
    """A single unambiguous match should mark the node complete and trigger the
    same streak/notification side effects as the REST endpoint (because it calls
    the same service function)."""
    n1 = make_node(client, auth, roadmap["id"], "Setup environment").json()

    rm_id = roadmap["id"]
    tc = _make_tool_call(
        "complete_node",
        {"roadmap_id": rm_id, "node_name": "setup env"},
        "call_cn_clear",
    )

    fake_client = _make_fake_client(
        _tool_response([tc]),
        _text_response("Done! I've marked 'Setup environment' as complete. Great work!"),
    )
    monkeypatch.setattr("app.chat.build_chat_client", lambda: fake_client)

    chat_resp = client.post(
        "/chat", json={"message": "Mark setup env as done"}, headers=auth
    )
    assert chat_resp.status_code == 200
    body = chat_resp.json()
    assert any(a["tool"] == "complete_node" for a in body["actions_taken"])
    cn_action = next(a for a in body["actions_taken"] if a["tool"] == "complete_node")
    assert "complete" in cn_action["result"].lower()

    # Node must actually be completed in the DB
    db_session.expire_all()
    db_n1 = db_session.get(Node, uuid.UUID(n1["id"]))
    assert db_n1 is not None and db_n1.completed

    # Streak side effect — a UserStreak row should exist for this user
    user_id = _user_id_from_auth(auth)
    streak = db_session.query(UserStreak).filter(UserStreak.user_id == user_id).first()
    assert streak is not None
    assert streak.current_streak >= 1


# ---------------------------------------------------------------------------
# Test 5 — no Groq key → graceful message, no 500
# ---------------------------------------------------------------------------


def test_no_groq_key_returns_graceful_message(client, auth, monkeypatch):
    """When build_chat_client() returns None the endpoint should return 200 with
    a friendly degraded reply rather than a 500 error."""
    monkeypatch.setattr("app.chat.build_chat_client", lambda: None)

    resp = client.post("/chat", json={"message": "Hello Nodi"}, headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"]  # non-empty
    assert "not" in body["reply"].lower() or "configured" in body["reply"].lower() or "set up" in body["reply"].lower()
    assert body["actions_taken"] == []


# ---------------------------------------------------------------------------
# Test 6 — chat history is persisted and passed on the next turn
# ---------------------------------------------------------------------------


def test_chat_history_persisted_and_sent_on_next_turn(client, auth, monkeypatch, db_session):
    """Two sequential requests.  After both:
    1. Two user messages and two assistant messages should be in chat_messages.
    2. The second Groq call receives the first exchange in its messages list.
    """
    recorded_messages: list[list[dict]] = []

    def capturing_create(*args, **kwargs):
        msgs = kwargs.get("messages", args[0] if args else [])
        recorded_messages.append([m.copy() if isinstance(m, dict) else m for m in msgs])
        return _text_response(f"Turn {len(recorded_messages)}: got it!")

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = capturing_create
    monkeypatch.setattr("app.chat.build_chat_client", lambda: fake_client)

    resp1 = client.post("/chat", json={"message": "First message"}, headers=auth)
    assert resp1.status_code == 200

    resp2 = client.post("/chat", json={"message": "Second message"}, headers=auth)
    assert resp2.status_code == 200

    # DB should have 4 rows: user, assistant, user, assistant
    user_id = _user_id_from_auth(auth)

    db_session.expire_all()
    rows = (
        db_session.query(ChatMessage)
        .filter(ChatMessage.user_id == user_id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    assert len(rows) == 4
    roles = [r.role.value for r in rows]
    assert roles == ["user", "assistant", "user", "assistant"]

    # The second Groq call should have received the first exchange in its history
    assert len(recorded_messages) == 2
    second_call_msgs = recorded_messages[1]
    contents = [m.get("content", "") for m in second_call_msgs if m.get("role") != "system"]
    assert "First message" in contents
    assert any("Turn 1" in str(c) for c in contents)
