"""Generation flow tests.

The Groq client is faked throughout: `app.generation.build_client` is
monkeypatched to return a `FakeGroq` that replays canned structured-output
payloads keyed by the json_schema name. No test here touches the network.
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from app import generation
from app.generation import GenerationError, run_generation
from tests.conftest import make_node


# --- fake Groq client ----------------------------------------------------


class FakeCompletions:
    def __init__(self, owner):
        self._owner = owner

    def create(self, *, model, messages, temperature, response_format):
        owner = self._owner
        spec = response_format["json_schema"]
        name = spec["name"]

        owner.calls.append(
            {
                "name": name,
                "model": model,
                "system": messages[0]["content"],
                "user": messages[1]["content"],
                "strict": spec["strict"],
                "schema": spec["schema"],
            }
        )

        if owner.error is not None:
            raise owner.error

        payload = owner.responses[name]
        if callable(payload):
            payload = payload(owner)
        if isinstance(payload, Exception):
            raise payload

        content = payload if isinstance(payload, str) else json.dumps(payload)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class FakeGroq:
    """Stands in for `groq.Groq`, responding from a dict of canned payloads."""

    def __init__(self, responses, error=None):
        self.responses = responses
        self.error = error
        self.calls = []
        self.chat = SimpleNamespace(completions=FakeCompletions(self))

    def calls_named(self, name):
        return [c for c in self.calls if c["name"] == name]


def _task(name, prev=None, within=None):
    return {
        "name": name,
        "description": f"Do {name}.",
        "time_estimate": "2 hours",
        "depends_on_previous_phase": prev or [],
        "depends_on_earlier_in_phase": within or [],
    }


FLAT_RESPONSES = {
    "goal_classification": {"type": "flat", "reasoning": "Errands are independent."},
    "flat_tasks": {
        "tasks": [
            {"name": "Buy milk", "description": "At the corner shop.", "time_estimate": "10 minutes"},
            {"name": "Post the parcel", "description": "At the post office.", "time_estimate": "20 minutes"},
            {"name": "Refill prescription", "description": "At the pharmacy.", "time_estimate": "15 minutes"},
        ]
    },
}

SEQUENTIAL_RESPONSES = {
    "goal_classification": {"type": "sequential", "reasoning": "Skills build on each other."},
    "roadmap_phases": {
        "phases": [
            {"name": "Fundamentals", "description": "Get the basics down."},
            {"name": "Practice", "description": "Build fluency."},
            {"name": "Repertoire", "description": "Learn real pieces."},
            {"name": "Performance", "description": "Play for people."},
        ]
    },
    # One payload per phase, handed out in call order.
    "phase_tasks": lambda owner: [
        {"tasks": [_task("Read the clef"), _task("Name the notes", within=[0])]},
        {"tasks": [_task("Play open strings", prev=[1]), _task("Play a scale", prev=[1], within=[0])]},
        {"tasks": [_task("Learn a minuet", prev=[1])]},
        {"tasks": [_task("Play for a friend", prev=[0])]},
    ][len(owner.calls_named("phase_tasks")) - 1],
}


@pytest.fixture()
def fake_groq(monkeypatch):
    """Install a FakeGroq and hand it back for assertions."""

    def install(responses, error=None):
        client = FakeGroq(responses, error=error)
        monkeypatch.setattr(generation, "build_client", lambda: client)
        return client

    return install


# --- no key configured ---------------------------------------------------


def test_without_an_api_key_generation_is_skipped(client, auth, caplog):
    """The default test environment has no key, so POST must not fail or hang."""
    resp = client.post("/roadmaps", json={"goal_text": "Learn the cello"}, headers=auth)
    assert resp.status_code == 201
    body = client.get(f"/roadmaps/{resp.json()['id']}", headers=auth).json()
    assert body["status"] == "pending"
    assert body["nodes"] == []
    assert body["error_message"] is None


# --- classification routing ---------------------------------------------


def test_classification_routes_a_flat_goal_to_the_flat_path(client, auth, fake_groq):
    groq = fake_groq(FLAT_RESPONSES)

    created = client.post(
        "/roadmaps", json={"goal_text": "Saturday errands", "type": "sequential"}, headers=auth
    ).json()
    assert created["status"] == "pending", "the request must return before generation runs"

    detail = client.get(f"/roadmaps/{created['id']}", headers=auth).json()
    assert detail["status"] == "done"
    # The classifier's answer overrides the type supplied on create.
    assert detail["type"] == "flat"

    assert [c["name"] for c in groq.calls] == ["goal_classification", "flat_tasks"]
    assert groq.calls_named("roadmap_phases") == []
    assert groq.calls_named("phase_tasks") == []


def test_classification_routes_a_sequential_goal_to_the_two_pass_path(client, auth, fake_groq):
    groq = fake_groq(SEQUENTIAL_RESPONSES)

    created = client.post(
        "/roadmaps", json={"goal_text": "Learn to play the cello", "type": "flat"}, headers=auth
    ).json()
    detail = client.get(f"/roadmaps/{created['id']}", headers=auth).json()

    assert detail["status"] == "done"
    assert detail["type"] == "sequential"
    assert len(groq.calls_named("roadmap_phases")) == 1
    # One call per phase.
    assert len(groq.calls_named("phase_tasks")) == 4
    assert groq.calls_named("flat_tasks") == []


# --- flat generation -----------------------------------------------------


def test_flat_generation_produces_ordered_nodes_with_no_dependencies(client, auth, fake_groq):
    fake_groq(FLAT_RESPONSES)

    created = client.post("/roadmaps", json={"goal_text": "Saturday errands"}, headers=auth).json()
    detail = client.get(f"/roadmaps/{created['id']}", headers=auth).json()

    nodes = detail["nodes"]
    assert [n["name"] for n in nodes] == ["Buy milk", "Post the parcel", "Refill prescription"]
    assert [n["order"] for n in nodes] == [0, 1, 2]
    assert all(n["depends_on"] == [] for n in nodes)
    assert all(n["completed"] is False for n in nodes)
    assert nodes[0]["time_estimate"] == "10 minutes"

    assert client.post(f"/roadmaps/{created['id']}/validate", headers=auth).json()["valid"] is True


# --- sequential generation ----------------------------------------------


def test_sequential_generation_produces_a_valid_acyclic_graph(client, auth, fake_groq):
    fake_groq(SEQUENTIAL_RESPONSES)

    created = client.post(
        "/roadmaps", json={"goal_text": "Learn to play the cello"}, headers=auth
    ).json()
    detail = client.get(f"/roadmaps/{created['id']}", headers=auth).json()
    nodes = detail["nodes"]

    assert len(nodes) == 6
    assert [n["order"] for n in nodes] == [0, 1, 2, 3, 4, 5]

    by_id = {n["id"]: n for n in nodes}
    by_name = {n["name"]: n for n in nodes}

    # First phase has no prerequisites; a within-phase edge still points backwards.
    assert by_name["Read the clef"]["depends_on"] == []
    assert by_name["Name the notes"]["depends_on"] == [by_name["Read the clef"]["id"]]

    # Cross-phase edges resolve to the named task in the previous phase.
    assert by_name["Play open strings"]["depends_on"] == [by_name["Name the notes"]["id"]]
    assert set(by_name["Play a scale"]["depends_on"]) == {
        by_name["Name the notes"]["id"],
        by_name["Play open strings"]["id"],
    }

    # Every edge stays inside the roadmap, and the graph is acyclic.
    all_ids = set(by_id)
    for node in nodes:
        assert set(node["depends_on"]) <= all_ids
    assert _is_acyclic({n["id"]: n["depends_on"] for n in nodes})

    report = client.post(f"/roadmaps/{created['id']}/validate", headers=auth).json()
    assert report == {"roadmap_id": created["id"], "valid": True, "problems": []}


def test_a_task_with_no_stated_link_is_chained_to_the_previous_phase(client, auth, fake_groq):
    responses = dict(SEQUENTIAL_RESPONSES)
    responses["roadmap_phases"] = {
        "phases": [
            {"name": "One", "description": "First."},
            {"name": "Two", "description": "Second."},
            {"name": "Three", "description": "Third."},
            {"name": "Four", "description": "Fourth."},
        ]
    }
    responses["phase_tasks"] = lambda owner: [
        {"tasks": [_task("A"), _task("B")]},
        {"tasks": [_task("Orphan")]},  # no depends_on_previous_phase at all
        {"tasks": [_task("C", prev=[0])]},
        {"tasks": [_task("D", prev=[0])]},
    ][len(owner.calls_named("phase_tasks")) - 1]
    fake_groq(responses)

    created = client.post("/roadmaps", json={"goal_text": "Something staged"}, headers=auth).json()
    nodes = client.get(f"/roadmaps/{created['id']}", headers=auth).json()["nodes"]
    by_name = {n["name"]: n for n in nodes}

    assert by_name["Orphan"]["depends_on"] == [by_name["B"]["id"]]


def test_out_of_range_model_indices_are_dropped_not_persisted(client, auth, fake_groq):
    """A hallucinated index must not become a dangling edge or a forward cycle."""
    responses = dict(SEQUENTIAL_RESPONSES)
    responses["roadmap_phases"] = {
        "phases": [
            {"name": "One", "description": "First."},
            {"name": "Two", "description": "Second."},
            {"name": "Three", "description": "Third."},
            {"name": "Four", "description": "Fourth."},
        ]
    }
    responses["phase_tasks"] = lambda owner: [
        {"tasks": [_task("A", prev=[3], within=[7])]},  # first phase: both bogus
        {"tasks": [_task("B", prev=[99]), _task("C", within=[5])]},
        {"tasks": [_task("D", prev=[0])]},
        {"tasks": [_task("E", prev=[0])]},
    ][len(owner.calls_named("phase_tasks")) - 1]
    fake_groq(responses)

    created = client.post("/roadmaps", json={"goal_text": "Something staged"}, headers=auth).json()
    detail = client.get(f"/roadmaps/{created['id']}", headers=auth).json()
    assert detail["status"] == "done"

    by_name = {n["name"]: n for n in detail["nodes"]}
    assert by_name["A"]["depends_on"] == []
    # Bogus indices dropped, then the fallback chain kicks in.
    assert by_name["B"]["depends_on"] == [by_name["A"]["id"]]
    assert by_name["C"]["depends_on"] == [by_name["A"]["id"]]

    assert client.post(f"/roadmaps/{created['id']}/validate", headers=auth).json()["valid"] is True


# --- structured output usage --------------------------------------------


def test_every_call_uses_strict_json_schema_and_the_configured_model(client, auth, fake_groq):
    groq = fake_groq(SEQUENTIAL_RESPONSES)
    client.post("/roadmaps", json={"goal_text": "Learn to play the cello"}, headers=auth)

    assert groq.calls, "expected at least one Groq call"
    for call in groq.calls:
        assert call["model"] == "llama-3.3-70b-versatile"
        assert call["strict"] is True
        schema = call["schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_phase_task_prompts_carry_adjacent_phase_context(client, auth, fake_groq):
    groq = fake_groq(SEQUENTIAL_RESPONSES)
    client.post("/roadmaps", json={"goal_text": "Learn to play the cello"}, headers=auth)

    first, second, _, last = groq.calls_named("phase_tasks")
    assert "This is the FIRST phase" in first["user"]
    assert "Practice" in first["user"]  # the next phase is named

    assert "previous phase ('Fundamentals')" in second["user"]
    assert "[0] Read the clef" in second["user"]  # previous phase's tasks, indexed
    assert "Repertoire" in second["user"]

    assert "previous phase ('Repertoire')" in last["user"]


# --- failure handling ----------------------------------------------------


def test_groq_api_failure_sets_status_failed_without_crashing(client, auth, fake_groq):
    fake_groq(FLAT_RESPONSES, error=RuntimeError("groq: 503 service unavailable"))

    resp = client.post("/roadmaps", json={"goal_text": "Learn the cello"}, headers=auth)
    assert resp.status_code == 201, "the background failure must not affect the response"

    detail = client.get(f"/roadmaps/{resp.json()['id']}", headers=auth).json()
    assert detail["status"] == "failed"
    assert "503 service unavailable" in detail["error_message"]
    assert detail["nodes"] == []

    status = client.get(
        f"/roadmaps/{resp.json()['id']}/generation-status", headers=auth
    ).json()
    assert status["status"] == "failed"
    assert status["error_message"]


def test_failure_midway_through_a_sequential_run_leaves_no_partial_graph(client, auth, fake_groq):
    responses = dict(SEQUENTIAL_RESPONSES)
    responses["phase_tasks"] = lambda owner: (
        {"tasks": [_task("A")]}
        if len(owner.calls_named("phase_tasks")) == 1
        else RuntimeError("groq: rate limited")
    )
    fake_groq(responses)

    created = client.post("/roadmaps", json={"goal_text": "Learn the cello"}, headers=auth).json()
    detail = client.get(f"/roadmaps/{created['id']}", headers=auth).json()

    assert detail["status"] == "failed"
    assert "rate limited" in detail["error_message"]
    assert detail["nodes"] == []


def test_unparseable_response_sets_status_failed(client, auth, fake_groq):
    fake_groq({"goal_classification": "this is not json"})

    created = client.post("/roadmaps", json={"goal_text": "Learn the cello"}, headers=auth).json()
    detail = client.get(f"/roadmaps/{created['id']}", headers=auth).json()
    assert detail["status"] == "failed"
    assert "unparseable JSON" in detail["error_message"]


def test_unknown_classification_value_sets_status_failed(client, auth, fake_groq):
    fake_groq({"goal_classification": {"type": "diagonal", "reasoning": "?"}})

    created = client.post("/roadmaps", json={"goal_text": "Learn the cello"}, headers=auth).json()
    detail = client.get(f"/roadmaps/{created['id']}", headers=auth).json()
    assert detail["status"] == "failed"
    assert "diagonal" in detail["error_message"]


def test_a_graph_that_fails_validation_is_not_saved(client, auth, fake_groq, monkeypatch):
    """Defensive backstop: if wiring ever produced a bad graph, discard it."""
    from app.schemas import ValidationProblem

    fake_groq(FLAT_RESPONSES)
    monkeypatch.setattr(
        generation,
        "validate_roadmap_graph",
        lambda db, roadmap: [ValidationProblem(type="cycle", message="synthetic cycle")],
        raising=False,
    )
    # run_generation imports the symbol locally, so patch it at the source too.
    import app.validation

    monkeypatch.setattr(
        app.validation,
        "validate_roadmap_graph",
        lambda db, roadmap: [ValidationProblem(type="cycle", message="synthetic cycle")],
    )

    created = client.post("/roadmaps", json={"goal_text": "Saturday errands"}, headers=auth).json()
    detail = client.get(f"/roadmaps/{created['id']}", headers=auth).json()

    assert detail["status"] == "failed"
    assert "synthetic cycle" in detail["error_message"]
    assert detail["nodes"] == []


def test_generation_for_a_missing_roadmap_is_a_no_op(fake_groq, client):
    fake_groq(FLAT_RESPONSES)
    run_generation(uuid.uuid4())  # must not raise


# --- regenerate ----------------------------------------------------------


def test_regenerate_replaces_the_existing_graph(client, auth, fake_groq):
    fake_groq(FLAT_RESPONSES)
    created = client.post("/roadmaps", json={"goal_text": "Saturday errands"}, headers=auth).json()
    first = client.get(f"/roadmaps/{created['id']}", headers=auth).json()
    assert first["status"] == "done"
    original_ids = {n["id"] for n in first["nodes"]}

    resp = client.post(f"/roadmaps/{created['id']}/regenerate", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending", "the response precedes regeneration"

    after = client.get(f"/roadmaps/{created['id']}", headers=auth).json()
    assert after["status"] == "done"
    assert len(after["nodes"]) == 3
    assert original_ids.isdisjoint({n["id"] for n in after["nodes"]}), "nodes were rebuilt"


def test_regenerate_clears_a_previous_error_message(client, auth, fake_groq, monkeypatch):
    fake_groq(FLAT_RESPONSES, error=RuntimeError("groq: 503"))
    created = client.post("/roadmaps", json={"goal_text": "Saturday errands"}, headers=auth).json()
    assert client.get(f"/roadmaps/{created['id']}", headers=auth).json()["status"] == "failed"

    # Second attempt succeeds.
    fake_groq(FLAT_RESPONSES)
    client.post(f"/roadmaps/{created['id']}/regenerate", headers=auth)

    detail = client.get(f"/roadmaps/{created['id']}", headers=auth).json()
    assert detail["status"] == "done"
    assert detail["error_message"] is None
    assert len(detail["nodes"]) == 3


def test_regenerate_discards_manually_added_nodes(client, auth, fake_groq):
    fake_groq(FLAT_RESPONSES)
    created = client.post("/roadmaps", json={"goal_text": "Saturday errands"}, headers=auth).json()
    nodes = client.get(f"/roadmaps/{created['id']}", headers=auth).json()["nodes"]

    manual = make_node(client, auth, created["id"], "Hand-added", depends_on=[nodes[0]["id"]])
    assert manual.status_code == 201

    client.post(f"/roadmaps/{created['id']}/regenerate", headers=auth)
    after = client.get(f"/roadmaps/{created['id']}", headers=auth).json()
    assert "Hand-added" not in [n["name"] for n in after["nodes"]]


# --- unit-level checks ---------------------------------------------------


def test_as_indices_coerces_and_drops_junk():
    assert generation._as_indices([0, 2, "3"]) == [0, 2, 3]
    assert generation._as_indices([True, None, 1.5, "x", {}]) == []
    assert generation._as_indices(None) == []
    assert generation._as_indices("nope") == []


def test_call_wraps_client_exceptions_in_generation_error():
    groq = FakeGroq({}, error=TimeoutError("connection timed out"))
    with pytest.raises(GenerationError, match="connection timed out"):
        generation._call(
            groq, name="goal_classification", schema={}, system="s", user="u"
        )


def _is_acyclic(edges: dict) -> bool:
    """Kahn's algorithm over {node_id: [prerequisite ids]}."""
    remaining = {k: set(v) for k, v in edges.items()}
    while remaining:
        ready = [k for k, deps in remaining.items() if not deps]
        if not ready:
            return False
        for k in ready:
            del remaining[k]
        for deps in remaining.values():
            deps.difference_update(ready)
    return True
