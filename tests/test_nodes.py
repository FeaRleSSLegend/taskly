import uuid

from tests.conftest import make_node


def test_create_node_returns_expected_shape(client, auth, roadmap):
    resp = make_node(
        client,
        auth,
        roadmap["id"],
        "Rent a cello",
        description="Find a local shop",
        time_estimate="2 hours",
    )
    assert resp.status_code == 201, resp.text
    node = resp.json()
    assert set(node) == {
        "id",
        "roadmap_id",
        "name",
        "phase",
        "description",
        "time_estimate",
        "depends_on",
        "completed",
        "order",
    }
    assert node["roadmap_id"] == roadmap["id"]
    assert node["depends_on"] == []
    assert node["completed"] is False
    assert node["order"] == 0


def test_node_order_auto_increments(client, auth, roadmap):
    orders = [make_node(client, auth, roadmap["id"], n).json()["order"] for n in "ABC"]
    assert orders == [0, 1, 2]


def test_create_node_with_dependencies(client, auth, roadmap):
    a = make_node(client, auth, roadmap["id"], "A").json()
    b = make_node(client, auth, roadmap["id"], "B").json()
    c = make_node(client, auth, roadmap["id"], "C", depends_on=[a["id"], b["id"]]).json()
    assert sorted(c["depends_on"]) == sorted([a["id"], b["id"]])

    detail = client.get(f"/roadmaps/{roadmap['id']}", headers=auth).json()
    assert len(detail["nodes"]) == 3
    embedded = next(n for n in detail["nodes"] if n["id"] == c["id"])
    assert sorted(embedded["depends_on"]) == sorted([a["id"], b["id"]])


def test_create_node_rejects_empty_name(client, auth, roadmap):
    assert make_node(client, auth, roadmap["id"], "").status_code == 422


def test_patch_node_fields(client, auth, roadmap):
    node = make_node(client, auth, roadmap["id"], "A").json()
    resp = client.patch(
        f"/roadmaps/{roadmap['id']}/nodes/{node['id']}",
        json={"name": "A renamed", "description": "why", "time_estimate": "3 days"},
        headers=auth,
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "A renamed"
    assert resp.json()["description"] == "why"
    assert resp.json()["time_estimate"] == "3 days"


def test_patch_node_replaces_dependencies(client, auth, roadmap):
    a = make_node(client, auth, roadmap["id"], "A").json()
    b = make_node(client, auth, roadmap["id"], "B").json()
    c = make_node(client, auth, roadmap["id"], "C", depends_on=[a["id"]]).json()

    resp = client.patch(
        f"/roadmaps/{roadmap['id']}/nodes/{c['id']}",
        json={"depends_on": [b["id"]]},
        headers=auth,
    )
    assert resp.status_code == 200
    assert resp.json()["depends_on"] == [b["id"]]

    cleared = client.patch(
        f"/roadmaps/{roadmap['id']}/nodes/{c['id']}", json={"depends_on": []}, headers=auth
    )
    assert cleared.json()["depends_on"] == []


def test_complete_and_uncomplete(client, auth, roadmap):
    node = make_node(client, auth, roadmap["id"], "A").json()
    done = client.post(f"/roadmaps/{roadmap['id']}/nodes/{node['id']}/complete", headers=auth)
    assert done.status_code == 200
    assert done.json()["completed"] is True

    undone = client.post(
        f"/roadmaps/{roadmap['id']}/nodes/{node['id']}/uncomplete", headers=auth
    )
    assert undone.json()["completed"] is False


def test_reorder_nodes(client, auth, roadmap):
    a = make_node(client, auth, roadmap["id"], "A").json()
    b = make_node(client, auth, roadmap["id"], "B").json()
    c = make_node(client, auth, roadmap["id"], "C").json()

    resp = client.patch(
        f"/roadmaps/{roadmap['id']}/nodes/reorder",
        json={
            "items": [
                {"node_id": c["id"], "order": 0},
                {"node_id": a["id"], "order": 1},
                {"node_id": b["id"], "order": 2},
            ]
        },
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert [n["name"] for n in resp.json()] == ["C", "A", "B"]

    detail = client.get(f"/roadmaps/{roadmap['id']}", headers=auth).json()
    assert [n["name"] for n in detail["nodes"]] == ["C", "A", "B"]


def test_reorder_rejects_foreign_node(client, auth, roadmap):
    other = client.post("/roadmaps", json={"goal_text": "Other"}, headers=auth).json()
    foreign = make_node(client, auth, other["id"], "Foreign").json()

    resp = client.patch(
        f"/roadmaps/{roadmap['id']}/nodes/reorder",
        json={"items": [{"node_id": foreign["id"], "order": 0}]},
        headers=auth,
    )
    assert resp.status_code == 400


def test_node_404s(client, auth, roadmap):
    bogus = uuid.uuid4()
    assert client.patch(
        f"/roadmaps/{roadmap['id']}/nodes/{bogus}", json={"name": "x"}, headers=auth
    ).status_code == 404
    assert client.delete(
        f"/roadmaps/{roadmap['id']}/nodes/{bogus}", headers=auth
    ).status_code == 404
    assert client.post(
        f"/roadmaps/{roadmap['id']}/nodes/{bogus}/complete", headers=auth
    ).status_code == 404


# --- deletion with dependents -------------------------------------------


def test_delete_node_fails_when_a_dependent_exists(client, auth, roadmap):
    a = make_node(client, auth, roadmap["id"], "Learn to read music").json()
    b = make_node(client, auth, roadmap["id"], "Play a scale", depends_on=[a["id"]]).json()

    resp = client.delete(f"/roadmaps/{roadmap['id']}/nodes/{a['id']}", headers=auth)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "Play a scale" in detail
    assert b["id"] in detail

    # Nothing was cascaded away.
    nodes = client.get(f"/roadmaps/{roadmap['id']}", headers=auth).json()["nodes"]
    assert {n["id"] for n in nodes} == {a["id"], b["id"]}


def test_delete_node_succeeds_after_dependency_is_removed(client, auth, roadmap):
    a = make_node(client, auth, roadmap["id"], "A").json()
    b = make_node(client, auth, roadmap["id"], "B", depends_on=[a["id"]]).json()

    assert client.delete(f"/roadmaps/{roadmap['id']}/nodes/{a['id']}", headers=auth).status_code == 409

    client.patch(
        f"/roadmaps/{roadmap['id']}/nodes/{b['id']}", json={"depends_on": []}, headers=auth
    )
    assert client.delete(f"/roadmaps/{roadmap['id']}/nodes/{a['id']}", headers=auth).status_code == 204

    nodes = client.get(f"/roadmaps/{roadmap['id']}", headers=auth).json()["nodes"]
    assert [n["id"] for n in nodes] == [b["id"]]


def test_deleting_a_leaf_node_drops_its_own_dependency_edges(client, auth, roadmap):
    a = make_node(client, auth, roadmap["id"], "A").json()
    b = make_node(client, auth, roadmap["id"], "B", depends_on=[a["id"]]).json()

    assert client.delete(f"/roadmaps/{roadmap['id']}/nodes/{b['id']}", headers=auth).status_code == 204
    # A is now deletable, meaning the edge really went away.
    assert client.delete(f"/roadmaps/{roadmap['id']}/nodes/{a['id']}", headers=auth).status_code == 204
