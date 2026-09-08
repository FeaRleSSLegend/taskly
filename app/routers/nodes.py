from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_owned_node, get_owned_roadmap
from app.models import Node, Roadmap
from app.schemas import NodeCreate, NodeOut, NodeUpdate, ReorderRequest
from app.validation import check_dangling_references, validate_roadmap_graph

router = APIRouter(prefix="/roadmaps/{roadmap_id}/nodes", tags=["nodes"])


def _problem_payload(problems) -> dict:
    return {
        "message": "Operation would leave the roadmap graph invalid",
        "problems": [p.model_dump(mode="json") for p in problems],
    }


def _resolve_dependencies(db: Session, roadmap: Roadmap, ids) -> list[Node]:
    """Turn a client-supplied depends_on list into Node objects, rejecting
    dangling and cross-roadmap references before anything is written."""
    unique_ids = list(dict.fromkeys(ids))
    problems = check_dangling_references(db, roadmap.id, unique_ids)
    if problems:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_problem_payload(problems))
    if not unique_ids:
        return []
    by_id = {n.id: n for n in db.query(Node).filter(Node.id.in_(unique_ids)).all()}
    return [by_id[i] for i in unique_ids]


def _commit_validated(db: Session, roadmap: Roadmap) -> None:
    """Flush the pending write, re-run the structural check, and roll back with
    a 400 if the write introduced a cycle or a bad reference."""
    db.flush()
    problems = validate_roadmap_graph(db, roadmap)
    if problems:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_problem_payload(problems))
    db.commit()


@router.post("", response_model=NodeOut, status_code=status.HTTP_201_CREATED)
def create_node(
    payload: NodeCreate,
    roadmap: Roadmap = Depends(get_owned_roadmap),
    db: Session = Depends(get_db),
):
    if payload.order is None:
        max_order = (
            db.query(func.max(Node.order)).filter(Node.roadmap_id == roadmap.id).scalar()
        )
        order = 0 if max_order is None else max_order + 1
    else:
        order = payload.order

    node = Node(
        roadmap_id=roadmap.id,
        name=payload.name,
        description=payload.description,
        time_estimate=payload.time_estimate,
        order=order,
    )
    node.depends_on = _resolve_dependencies(db, roadmap, payload.depends_on)
    db.add(node)
    _commit_validated(db, roadmap)
    db.refresh(node)
    return node


# Declared before /{node_id} so "reorder" is not swallowed as a node id.
@router.patch("/reorder", response_model=list[NodeOut])
def reorder_nodes(
    payload: ReorderRequest,
    roadmap: Roadmap = Depends(get_owned_roadmap),
    db: Session = Depends(get_db),
):
    requested = {item.node_id: item.order for item in payload.items}
    nodes = db.query(Node).filter(Node.roadmap_id == roadmap.id).all()
    known = {n.id: n for n in nodes}

    unknown = [str(nid) for nid in requested if nid not in known]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Nodes not in this roadmap: {', '.join(unknown)}",
        )

    for node_id, order in requested.items():
        known[node_id].order = order
    db.commit()

    return (
        db.query(Node)
        .filter(Node.roadmap_id == roadmap.id)
        .order_by(Node.order, Node.name)
        .all()
    )


@router.patch("/{node_id}", response_model=NodeOut)
def update_node(
    payload: NodeUpdate,
    node: Node = Depends(get_owned_node),
    roadmap: Roadmap = Depends(get_owned_roadmap),
    db: Session = Depends(get_db),
):
    data = payload.model_dump(exclude_unset=True)
    if "depends_on" in data:
        depends_on = data.pop("depends_on")
        node.depends_on = _resolve_dependencies(db, roadmap, depends_on or [])
    for field, value in data.items():
        setattr(node, field, value)
    _commit_validated(db, roadmap)
    db.refresh(node)
    return node


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_node(
    node: Node = Depends(get_owned_node),
    roadmap: Roadmap = Depends(get_owned_roadmap),
    db: Session = Depends(get_db),
):
    dependents = list(node.dependents)
    if dependents:
        names = ", ".join(f"'{d.name}' ({d.id})" for d in dependents)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot delete node '{node.name}': {len(dependents)} other node(s) "
                f"depend on it: {names}. Remove those dependencies first."
            ),
        )
    node.depends_on.clear()
    db.delete(node)
    _commit_validated(db, roadmap)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{node_id}/complete", response_model=NodeOut)
def complete_node(node: Node = Depends(get_owned_node), db: Session = Depends(get_db)):
    node.completed = True
    db.commit()
    db.refresh(node)
    return node


@router.post("/{node_id}/uncomplete", response_model=NodeOut)
def uncomplete_node(node: Node = Depends(get_owned_node), db: Session = Depends(get_db)):
    node.completed = False
    db.commit()
    db.refresh(node)
    return node
