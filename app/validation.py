"""Structural validation of a roadmap's node graph.

Run automatically after every node create / edit / delete (see
`app.routers.nodes`), and exposed manually via POST /roadmaps/{id}/validate.
"""

from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Node, Roadmap
from app.schemas import ValidationProblem


def validate_roadmap_graph(db: Session, roadmap: Roadmap) -> list[ValidationProblem]:
    problems: list[ValidationProblem] = []

    nodes: list[Node] = list(
        db.query(Node).filter(Node.roadmap_id == roadmap.id).order_by(Node.order).all()
    )
    in_roadmap: set[UUID] = {n.id for n in nodes}

    # edges[node] = set of prerequisite ids that actually live in this roadmap
    edges: dict[UUID, set[UUID]] = {}

    for node in nodes:
        local: set[UUID] = set()
        for prereq in node.depends_on:
            if prereq.id == node.id:
                problems.append(
                    ValidationProblem(
                        type="self_reference",
                        message=f"Node '{node.name}' depends on itself.",
                        node_ids=[node.id],
                    )
                )
                continue
            if prereq.id not in in_roadmap:
                # The FK guarantees the row exists, so anything not in this
                # roadmap belongs to another one.
                problems.append(
                    ValidationProblem(
                        type="foreign_reference",
                        message=(
                            f"Node '{node.name}' depends on node {prereq.id}, "
                            "which belongs to a different roadmap."
                        ),
                        node_ids=[node.id, prereq.id],
                    )
                )
                continue
            local.add(prereq.id)
        edges[node.id] = local

    for cycle in _find_cycles(edges):
        names = " -> ".join(_name_of(nodes, nid) for nid in cycle)
        problems.append(
            ValidationProblem(
                type="cycle",
                message=f"Dependency cycle detected: {names}",
                node_ids=list(cycle),
            )
        )

    return problems


def check_dangling_references(
    db: Session, roadmap_id: UUID, referenced_ids: list[UUID]
) -> list[ValidationProblem]:
    """Validate ids supplied by a client *before* they are attached.

    Distinguishes ids that do not exist at all from ids that exist but live in
    another roadmap, which `validate_roadmap_graph` cannot tell apart once the
    foreign key has been enforced.
    """
    problems: list[ValidationProblem] = []
    unique_ids = list(dict.fromkeys(referenced_ids))
    if not unique_ids:
        return problems

    found = {
        n.id: n.roadmap_id
        for n in db.query(Node.id, Node.roadmap_id).filter(Node.id.in_(unique_ids)).all()
    }
    for nid in unique_ids:
        if nid not in found:
            problems.append(
                ValidationProblem(
                    type="dangling_reference",
                    message=f"depends_on references node {nid}, which does not exist.",
                    node_ids=[nid],
                )
            )
        elif found[nid] != roadmap_id:
            problems.append(
                ValidationProblem(
                    type="foreign_reference",
                    message=(
                        f"depends_on references node {nid}, which belongs to a "
                        "different roadmap."
                    ),
                    node_ids=[nid],
                )
            )
    return problems


def _name_of(nodes: list[Node], node_id: UUID) -> str:
    for n in nodes:
        if n.id == node_id:
            return n.name
    return str(node_id)


def _find_cycles(edges: dict[UUID, set[UUID]]) -> list[list[UUID]]:
    """Iterative DFS returning one representative cycle per strongly connected
    region reached. Each cycle is a list of node ids in traversal order."""
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[UUID, int] = {n: WHITE for n in edges}
    cycles: list[list[UUID]] = []
    seen_cycles: set[frozenset[UUID]] = set()

    for start in edges:
        if color[start] != WHITE:
            continue
        stack: list[tuple[UUID, list[UUID]]] = [(start, list(edges[start]))]
        path: list[UUID] = [start]
        color[start] = GREY

        while stack:
            node, remaining = stack[-1]
            if not remaining:
                color[node] = BLACK
                stack.pop()
                path.pop()
                continue
            nxt = remaining.pop()
            if color.get(nxt) == GREY:
                cycle = path[path.index(nxt):] + [nxt]
                key = frozenset(cycle)
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    cycles.append(cycle)
            elif color.get(nxt) == WHITE:
                color[nxt] = GREY
                path.append(nxt)
                stack.append((nxt, list(edges[nxt])))

    return cycles
