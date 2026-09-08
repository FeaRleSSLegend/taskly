# Backend API Specification: AI Task Roadmap Generator

## Stack and Core Decisions

- **Framework:** FastAPI
- **Auth:** JWT with expiry. No refresh token flow for v1, a straightforward access token that expires and requires re-login. Simpler to build and enough for a v1.
- **Dependencies:** embedded on the node itself as a `depends_on` list of node IDs, not modeled as a separate edges resource. Simpler schema, and there's no current need for edge-level metadata.
- **Node deletion with dependents:** hard fail with a clear error if other nodes list the target node in their `depends_on`. The user has to resolve the dependency manually before deleting. No silent cascade, since that could quietly break the intended order of the graph.
- **Roadmap-task relationship:** one roadmap per task, multiple tasks per user. A "roadmap" and a "task" (in the product sense of a goal) are the same object in the API, referred to as `roadmap` throughout.

## Auth

| Method | Path | Description |
|---|---|---|
| POST | `/auth/register` | Create a user with email and password. Returns user object and access token. |
| POST | `/auth/login` | Returns access token on valid credentials. |
| POST | `/auth/logout` | Invalidate the current token (if using a blocklist) or a no-op if relying purely on expiry. |
| GET | `/auth/me` | Return the current authenticated user's profile. |

## Roadmaps

| Method | Path | Description |
|---|---|---|
| POST | `/roadmaps` | Create a roadmap from a goal string. Kicks off generation asynchronously and returns immediately with status `generating`. Do not block the request on the LLM calls. |
| GET | `/roadmaps` | List all roadmaps for the current user. Return title, goal type (sequential or flat), progress percentage, and status per roadmap. |
| GET | `/roadmaps/{roadmap_id}` | Full roadmap detail, including all nodes with their embedded `depends_on` lists. |
| PATCH | `/roadmaps/{roadmap_id}` | Edit roadmap metadata: title, goal text. |
| DELETE | `/roadmaps/{roadmap_id}` | Delete a roadmap and its nodes. |
| POST | `/roadmaps/{roadmap_id}/regenerate` | Re-run the decomposition engine on the same goal text, replacing the existing graph. |
| GET | `/roadmaps/{roadmap_id}/generation-status` | Poll while a roadmap is generating. Status values: `pending`, `generating_phases`, `generating_tasks`, `done`, `failed`. Needed because two-pass generation is not instant. |
| GET | `/roadmaps/{roadmap_id}/progress` | Percentage complete. Can be a field on the roadmap object returned elsewhere instead of a standalone endpoint. |

## Nodes

| Method | Path | Description |
|---|---|---|
| POST | `/roadmaps/{roadmap_id}/nodes` | Manually add a node: name, description, time estimate, optional `depends_on` list of existing node IDs. |
| PATCH | `/roadmaps/{roadmap_id}/nodes/{node_id}` | Edit name, description, estimate, or `depends_on`. |
| DELETE | `/roadmaps/{roadmap_id}/nodes/{node_id}` | Fails with an error if other nodes depend on this one. Caller must remove those dependencies first. |
| POST | `/roadmaps/{roadmap_id}/nodes/{node_id}/complete` | Mark a node complete. |
| POST | `/roadmaps/{roadmap_id}/nodes/{node_id}/uncomplete` | Revert a node to incomplete. |
| PATCH | `/roadmaps/{roadmap_id}/nodes/reorder` | Batch reorder, mainly for the flat checklist case where order is display order, not a dependency. |

## Validation

| Method | Path | Description |
|---|---|---|
| POST | `/roadmaps/{roadmap_id}/validate` | Run the structural check: no cycles, no orphaned nodes, no dangling `depends_on` references. Since full manual editing is in scope, a user can create an invalid graph by hand. Call this automatically server-side after every node create, edit, or delete rather than relying on the frontend to trigger it. |

## Dashboard

| Method | Path | Description |
|---|---|---|
| GET | `/dashboard` | Aggregate view across all of a user's roadmaps: title, progress, status, for a home screen listing every active goal. |

## Node Object Shape (reference for Claude Code)

```json
{
  "id": "uuid",
  "roadmap_id": "uuid",
  "name": "string",
  "description": "string",
  "time_estimate": "string or minutes, decide one",
  "depends_on": ["node_id", "node_id"],
  "completed": false,
  "order": 0
}
```

`order` matters for flat/checklist roadmaps where there's no real dependency, just a display sequence. For sequential roadmaps, position in the graph comes from `depends_on`, and `order` can be ignored or used as a tiebreaker within a phase.

## Roadmap Object Shape (reference for Claude Code)

```json
{
  "id": "uuid",
  "user_id": "uuid",
  "title": "string",
  "goal_text": "string",
  "type": "sequential | flat",
  "status": "pending | generating_phases | generating_tasks | done | failed",
  "progress_percentage": 0,
  "nodes": ["array of node objects"]
}
```
