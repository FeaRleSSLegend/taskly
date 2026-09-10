from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

from app.models import UserStreak
from tests.conftest import make_node


@pytest.fixture()
def mock_resend(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr("app.notifications.get_settings", lambda: MagicMock(resend_api_key="test_key"))
    monkeypatch.setattr("resend.Emails.send", mock)
    return mock


@pytest.fixture()
def db_session(db_engine):
    TestingSession = sessionmaker(bind=db_engine, autoflush=False, autocommit=False, future=True)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def test_streak_endpoint_defaults_to_zero(client, auth):
    resp = client.get("/streak", headers=auth)
    assert resp.status_code == 200
    data = resp.json()
    assert data["current_streak"] == 0
    assert data["longest_streak"] == 0
    assert data["last_active_date"] is None


def test_streak_increments_and_resets(client, auth, db_session, roadmap):
    today = datetime.now(timezone.utc).date()
    n1 = make_node(client, auth, roadmap["id"], "Node 1").json()
    n2 = make_node(client, auth, roadmap["id"], "Node 2").json()

    # Complete first node today -> current_streak=1, longest_streak=1
    client.post(f"/roadmaps/{roadmap['id']}/nodes/{n1['id']}/complete", headers=auth)
    s = client.get("/streak", headers=auth).json()
    assert s["current_streak"] == 1
    assert s["longest_streak"] == 1
    assert s["last_active_date"] == str(today)

    # Complete second node same day -> streak remains 1
    client.post(f"/roadmaps/{roadmap['id']}/nodes/{n2['id']}/complete", headers=auth)
    s = client.get("/streak", headers=auth).json()
    assert s["current_streak"] == 1
    assert s["longest_streak"] == 1

    # Simulate last_active_date was yesterday
    yesterday = today - timedelta(days=1)
    streak_row = db_session.query(UserStreak).first()
    streak_row.last_active_date = yesterday
    db_session.commit()

    n3 = make_node(client, auth, roadmap["id"], "Node 3").json()
    client.post(f"/roadmaps/{roadmap['id']}/nodes/{n3['id']}/complete", headers=auth)
    s = client.get("/streak", headers=auth).json()
    assert s["current_streak"] == 2
    assert s["longest_streak"] == 2

    # Simulate streak gap (last active 3 days ago)
    streak_row = db_session.query(UserStreak).first()
    streak_row.last_active_date = today - timedelta(days=3)
    db_session.commit()

    n4 = make_node(client, auth, roadmap["id"], "Node 4").json()
    client.post(f"/roadmaps/{roadmap['id']}/nodes/{n4['id']}/complete", headers=auth)
    s = client.get("/streak", headers=auth).json()
    assert s["current_streak"] == 1
    assert s["longest_streak"] == 2


def test_notification_preferences_get_and_patch(client, auth):
    pref = client.get("/notifications/preferences", headers=auth).json()
    assert pref["email_enabled"] is True
    assert pref["milestone_notifications"] is True

    updated = client.patch(
        "/notifications/preferences",
        json={"email_enabled": False},
        headers=auth,
    ).json()
    assert updated["email_enabled"] is False
    assert updated["milestone_notifications"] is True


def test_milestone_notification_and_no_double_fire(client, auth, mock_resend, roadmap):
    n1 = make_node(client, auth, roadmap["id"], "Node 1").json()
    n2 = make_node(client, auth, roadmap["id"], "Node 2").json()

    # Complete n1 -> progress goes from 0% to 50%, crossing 25% and 50%
    client.post(f"/roadmaps/{roadmap['id']}/nodes/{n1['id']}/complete", headers=auth)

    # Two notifications (25% and 50%) should be generated and emailed
    notes = client.get("/notifications", headers=auth).json()
    assert len(notes) == 2
    assert notes[0]["type"] == "milestone"
    assert "25%" in notes[0]["message"]
    assert "50%" in notes[1]["message"]
    assert mock_resend.call_count == 2

    # GET /notifications delivered=true, second call returns empty
    notes_second = client.get("/notifications", headers=auth).json()
    assert notes_second == []

    # Uncomplete n1 -> progress back to 0% (no notification on uncomplete)
    client.post(f"/roadmaps/{roadmap['id']}/nodes/{n1['id']}/uncomplete", headers=auth)
    assert client.get("/notifications", headers=auth).json() == []

    # Re-complete n1 -> progress goes back to 50%, but 25% and 50% notifications already exist for this roadmap
    mock_resend.reset_mock()
    client.post(f"/roadmaps/{roadmap['id']}/nodes/{n1['id']}/complete", headers=auth)
    assert client.get("/notifications", headers=auth).json() == []
    assert mock_resend.call_count == 0


def test_disabling_milestone_notifications_suppresses_both_in_app_and_email(
    client, auth, mock_resend, roadmap
):
    client.patch(
        "/notifications/preferences",
        json={"milestone_notifications": False},
        headers=auth,
    )
    n1 = make_node(client, auth, roadmap["id"], "Node 1").json()
    client.post(f"/roadmaps/{roadmap['id']}/nodes/{n1['id']}/complete", headers=auth)

    assert client.get("/notifications", headers=auth).json() == []
    assert mock_resend.call_count == 0


def test_disabling_email_enabled_suppresses_only_email(
    client, auth, mock_resend, roadmap
):
    client.patch(
        "/notifications/preferences",
        json={"email_enabled": False, "milestone_notifications": True},
        headers=auth,
    )
    n1 = make_node(client, auth, roadmap["id"], "Node 1").json()
    n2 = make_node(client, auth, roadmap["id"], "Node 2").json()
    n3 = make_node(client, auth, roadmap["id"], "Node 3").json()
    n4 = make_node(client, auth, roadmap["id"], "Node 4").json()
    client.post(f"/roadmaps/{roadmap['id']}/nodes/{n1['id']}/complete", headers=auth)

    notes = client.get("/notifications", headers=auth).json()
    assert len(notes) == 1
    assert notes[0]["type"] == "milestone"
    assert mock_resend.call_count == 0
