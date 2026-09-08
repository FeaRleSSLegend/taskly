import uuid

from tests.conftest import make_node


def test_create_roadmap_is_stubbed_as_pending_with_no_nodes(client, auth):
    resp = client.post(
        "/roadmaps", json={"goal_text": "Ship a side project in 30 days"}, headers=auth
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["type"] == "sequential"
    assert body["nodes"] == []
    assert body["progress_percentage"] == 0
    assert body["title"] == "Ship a side project in 30 days"


def test_create_roadmap_accepts_explicit_title_and_type(client, auth):
    resp = client.post(
        "/roadmaps",
        json={"goal_text": "Groceries and chores", "title": "Weekend", "type": "flat"},
        headers=auth,
    )
    assert resp.status_code == 201
    assert resp.json()["title"] == "Weekend"
    assert resp.json()["type"] == "flat"


def test_create_roadmap_derives_truncated_title_from_long_goal(client, auth):
    goal = "x" * 200
    body = client.post("/roadmaps", json={"goal_text": goal}, headers=auth).json()
    assert len(body["title"]) <= 80
    assert body["title"].endswith("...")


def test_create_roadmap_rejects_empty_goal(client, auth):
    assert client.post("/roadmaps", json={"goal_text": ""}, headers=auth).status_code == 422


def test_list_and_get_roadmap(client, auth, roadmap):
    listed = client.get("/roadmaps", headers=auth)
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["id"] == roadmap["id"]
    assert set(rows[0]) >= {"title", "type", "status", "progress_percentage"}

    detail = client.get(f"/roadmaps/{roadmap['id']}", headers=auth)
    assert detail.status_code == 200
    assert detail.json()["goal_text"] == "Learn to play the cello"


def test_patch_roadmap_updates_title_and_goal(client, auth, roadmap):
    resp = client.patch(
        f"/roadmaps/{roadmap['id']}",
        json={"title": "Cello, seriously", "goal_text": "Play Bach's first suite"},
        headers=auth,
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Cello, seriously"
    assert resp.json()["goal_text"] == "Play Bach's first suite"


def test_patch_roadmap_partial_update_leaves_other_fields(client, auth, roadmap):
    resp = client.patch(
        f"/roadmaps/{roadmap['id']}", json={"title": "Only the title"}, headers=auth
    )
    assert resp.status_code == 200
    assert resp.json()["goal_text"] == roadmap["goal_text"]


def test_delete_roadmap_cascades_to_nodes(client, auth, roadmap):
    a = make_node(client, auth, roadmap["id"], "A").json()
    make_node(client, auth, roadmap["id"], "B", depends_on=[a["id"]])

    assert client.delete(f"/roadmaps/{roadmap['id']}", headers=auth).status_code == 204
    assert client.get(f"/roadmaps/{roadmap['id']}", headers=auth).status_code == 404
    assert client.get("/roadmaps", headers=auth).json() == []


def test_missing_roadmap_returns_404(client, auth):
    assert client.get(f"/roadmaps/{uuid.uuid4()}", headers=auth).status_code == 404


def test_generation_status_endpoint(client, auth, roadmap):
    resp = client.get(f"/roadmaps/{roadmap['id']}/generation-status", headers=auth)
    assert resp.status_code == 200
    assert resp.json() == {
        "roadmap_id": roadmap["id"],
        "status": "pending",
        "error_message": None,
    }


def test_regenerate_clears_nodes_and_resets_status(client, auth, roadmap):
    a = make_node(client, auth, roadmap["id"], "A").json()
    make_node(client, auth, roadmap["id"], "B", depends_on=[a["id"]])
    client.patch(f"/roadmaps/{roadmap['id']}", json={"title": "T"}, headers=auth)

    resp = client.post(f"/roadmaps/{roadmap['id']}/regenerate", headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["nodes"] == []
    assert body["goal_text"] == roadmap["goal_text"]


def test_progress_percentage(client, auth, roadmap):
    empty = client.get(f"/roadmaps/{roadmap['id']}/progress", headers=auth).json()
    assert empty["progress_percentage"] == 0
    assert empty["total_nodes"] == 0

    a = make_node(client, auth, roadmap["id"], "A").json()
    make_node(client, auth, roadmap["id"], "B")
    make_node(client, auth, roadmap["id"], "C")

    client.post(f"/roadmaps/{roadmap['id']}/nodes/{a['id']}/complete", headers=auth)
    prog = client.get(f"/roadmaps/{roadmap['id']}/progress", headers=auth).json()
    assert prog["total_nodes"] == 3
    assert prog["completed_nodes"] == 1
    assert prog["progress_percentage"] == 33.33

    summary = client.get("/roadmaps", headers=auth).json()[0]
    assert summary["progress_percentage"] == 33.33


def test_dashboard_lists_user_roadmaps(client, auth, roadmap):
    client.post("/roadmaps", json={"goal_text": "Second goal"}, headers=auth)
    resp = client.get("/dashboard", headers=auth)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    for row in rows:
        assert set(row) >= {"title", "progress_percentage", "status"}


# --- ownership scoping ---------------------------------------------------


def test_roadmaps_are_scoped_to_the_owner(client, auth, other_auth, roadmap):
    assert client.get("/roadmaps", headers=other_auth).json() == []
    assert client.get("/dashboard", headers=other_auth).json() == []


def test_other_user_cannot_read_or_modify_roadmap(client, auth, other_auth, roadmap):
    rid = roadmap["id"]
    assert client.get(f"/roadmaps/{rid}", headers=other_auth).status_code == 404
    assert client.patch(
        f"/roadmaps/{rid}", json={"title": "hijacked"}, headers=other_auth
    ).status_code == 404
    assert client.delete(f"/roadmaps/{rid}", headers=other_auth).status_code == 404
    assert client.post(f"/roadmaps/{rid}/regenerate", headers=other_auth).status_code == 404
    assert client.get(f"/roadmaps/{rid}/progress", headers=other_auth).status_code == 404
    assert client.post(f"/roadmaps/{rid}/validate", headers=other_auth).status_code == 404

    # ... and the roadmap is untouched
    assert client.get(f"/roadmaps/{rid}", headers=auth).json()["title"] == roadmap["title"]


def test_other_user_cannot_touch_nodes(client, auth, other_auth, roadmap):
    rid = roadmap["id"]
    node = make_node(client, auth, rid, "A").json()

    assert make_node(client, other_auth, rid, "sneaky").status_code == 404
    assert client.patch(
        f"/roadmaps/{rid}/nodes/{node['id']}", json={"name": "x"}, headers=other_auth
    ).status_code == 404
    assert client.delete(
        f"/roadmaps/{rid}/nodes/{node['id']}", headers=other_auth
    ).status_code == 404
    assert client.post(
        f"/roadmaps/{rid}/nodes/{node['id']}/complete", headers=other_auth
    ).status_code == 404
