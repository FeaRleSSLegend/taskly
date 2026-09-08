import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("JWT_SECRET", "test-secret")
# Hard-set, not setdefault: the test suite must never reach the live Groq API,
# even when a real key is present in the developer's environment or .env file.
# With no key, `build_client()` returns None and generation is a no-op, so the
# pre-existing tests keep seeing freshly created roadmaps in `pending`.
os.environ["GROQ_API_KEY"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.event import listens_for  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import database  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402

get_settings.cache_clear()


@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def client(db_engine):
    TestingSession = sessionmaker(bind=db_engine, autoflush=False, autocommit=False, future=True)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    # The background generation task opens its own session via
    # `database.session_scope()`, which the request-scoped `get_db` override
    # cannot reach. Point the module-level factory at the test engine too.
    original_session_local = database.SessionLocal
    database.SessionLocal = TestingSession
    try:
        with TestClient(app) as c:
            yield c
    finally:
        database.SessionLocal = original_session_local
        app.dependency_overrides.clear()


def register(client, email=None, password="supersecret123"):
    """Register a user and return (auth_headers, user_json)."""
    email = email or f"user-{uuid.uuid4().hex[:8]}@example.com"
    resp = client.post("/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body["user"]


@pytest.fixture()
def auth(client):
    headers, user = register(client)
    return headers


@pytest.fixture()
def other_auth(client):
    headers, user = register(client)
    return headers


@pytest.fixture()
def roadmap(client, auth):
    resp = client.post(
        "/roadmaps",
        json={"goal_text": "Learn to play the cello", "type": "sequential"},
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_node(client, auth, roadmap_id, name, depends_on=None, **kwargs):
    payload = {"name": name, "depends_on": depends_on or [], **kwargs}
    return client.post(f"/roadmaps/{roadmap_id}/nodes", json=payload, headers=auth)
