import uuid

from tests.conftest import make_node


def test_validate_returns_clean_report_for_acyclic_graph(client, auth, roadmap):
    a = make_node(client, auth, roadmap["id"], "A").json()
    b = make_node(client, auth, roadmap["id"], "B", depends_on=[a["id"]]).json()
    make_node(client, auth, roadmap["id"], "C", depends_on=[b["id"]])

    resp = client.post(f"/roadmaps/{roadmap['id']}/validate", headers=auth)
    assert resp.status_code == 200
    assert resp.json() == {"roadmap_id": roadmap["id"], "valid": True, "problems": []}


def test_validate_on_empty_roadmap_is_clean(client, auth, roadmap):
    assert client.post(f"/roadmaps/{roadmap['id']}/validate", headers=auth).json()["valid"] is True


def test_patch_rejects_two_node_cycle(client, auth, roadmap):
    a = make_node(client, auth, roadmap["id"], "A").json()
    b = make_node(client, auth, roadmap["id"], "B", depends_on=[a["id"]]).json()

    # A -> B -> A
    resp = client.patch(
        f"/roadmaps/{roadmap['id']}/nodes/{a['id']}",
        json={"depends_on": [b["id"]]},
        headers=auth,
    )
    assert resp.status_code == 400, resp.text
    problems = resp.json()["detail"]["problems"]
    assert [p["type"] for p in problems] == ["cycle"]

    # The rejected write was rolled back: the graph is untouched and still valid.
    assert client.post(f"/roadmaps/{roadmap['id']}/validate", headers=auth).json()["valid"] is True
    fresh = client.get(f"/roadmaps/{roadmap['id']}", headers=auth).json()
    a_now = next(n for n in fresh["nodes"] if n["id"] == a["id"])
    assert a_now["depends_on"] == []


def test_patch_rejects_longer_cycle(client, auth, roadmap):
    a = make_node(client, auth, roadmap["id"], "A").json()
    b = make_node(client, auth, roadmap["id"], "B", depends_on=[a["id"]]).json()
    c = make_node(client, auth, roadmap["id"], "C", depends_on=[b["id"]]).json()

    resp = client.patch(
        f"/roadmaps/{roadmap['id']}/nodes/{a['id']}",
        json={"depends_on": [c["id"]]},
        headers=auth,
    )
    assert resp.status_code == 400
    problem = resp.json()["detail"]["problems"][0]
    assert problem["type"] == "cycle"
    assert {a["id"], b["id"], c["id"]} <= set(problem["node_ids"])


def test_node_cannot_depend_on_itself(client, auth, roadmap):
    a = make_node(client, auth, roadmap["id"], "A").json()
    resp = client.patch(
        f"/roadmaps/{roadmap['id']}/nodes/{a['id']}",
        json={"depends_on": [a["id"]]},
        headers=auth,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["problems"][0]["type"] == "self_reference"


def test_create_rejects_dangling_dependency(client, auth, roadmap):
    ghost = str(uuid.uuid4())
    resp = make_node(client, auth, roadmap["id"], "A", depends_on=[ghost])
    assert resp.status_code == 400
    problems = resp.json()["detail"]["problems"]
    assert problems[0]["type"] == "dangling_reference"
    assert ghost in problems[0]["message"]

    assert client.get(f"/roadmaps/{roadmap['id']}", headers=auth).json()["nodes"] == []


def test_create_rejects_dependency_on_another_roadmaps_node(client, auth, roadmap):
    other = client.post("/roadmaps", json={"goal_text": "Other goal"}, headers=auth).json()
    foreign = make_node(client, auth, other["id"], "Foreign").json()

    resp = make_node(client, auth, roadmap["id"], "A", depends_on=[foreign["id"]])
    assert resp.status_code == 400
    assert resp.json()["detail"]["problems"][0]["type"] == "foreign_reference"


def test_patch_rejects_dependency_on_another_roadmaps_node(client, auth, roadmap):
    other = client.post("/roadmaps", json={"goal_text": "Other goal"}, headers=auth).json()
    foreign = make_node(client, auth, other["id"], "Foreign").json()
    a = make_node(client, auth, roadmap["id"], "A").json()

    resp = client.patch(
        f"/roadmaps/{roadmap['id']}/nodes/{a['id']}",
        json={"depends_on": [foreign["id"]]},
        headers=auth,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["problems"][0]["type"] == "foreign_reference"


def test_duplicate_dependency_ids_are_deduplicated(client, auth, roadmap):
    a = make_node(client, auth, roadmap["id"], "A").json()
    b = make_node(client, auth, roadmap["id"], "B", depends_on=[a["id"], a["id"]])
    assert b.status_code == 201
    assert b.json()["depends_on"] == [a["id"]]


def test_diamond_graph_is_valid(client, auth, roadmap):
    a = make_node(client, auth, roadmap["id"], "A").json()
    b = make_node(client, auth, roadmap["id"], "B", depends_on=[a["id"]]).json()
    c = make_node(client, auth, roadmap["id"], "C", depends_on=[a["id"]]).json()
    d = make_node(client, auth, roadmap["id"], "D", depends_on=[b["id"], c["id"]])
    assert d.status_code == 201
    assert client.post(f"/roadmaps/{roadmap['id']}/validate", headers=auth).json()["valid"] is True
