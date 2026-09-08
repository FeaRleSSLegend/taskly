# AI Task Roadmap Generator — Backend

FastAPI + SQLAlchemy 2.0 + PostgreSQL. Managed with `uv`.

## What was implemented

**Auth** (`app/routers/auth.py`, `app/security.py`)
- `POST /auth/register` — email + password, returns the user object and an access token (201).
- `POST /auth/login` — returns an access token.
- `GET /auth/me` — current user profile.
- JWT (HS256) with a 24h expiry and no refresh flow. Passwords hashed with the `bcrypt`
  library directly (`bcrypt.hashpw` / `bcrypt.checkpw`); passlib is not used.
- Every route except register/login is guarded by a `get_current_user` dependency that
  validates the bearer token and resolves the user.

**Roadmaps** (`app/routers/roadmaps.py`)
- `POST /roadmaps`, `GET /roadmaps`, `GET /roadmaps/{id}`, `PATCH /roadmaps/{id}`,
  `DELETE /roadmaps/{id}` (cascades to nodes), `POST /roadmaps/{id}/regenerate`,
  `GET /roadmaps/{id}/generation-status`, `GET /roadmaps/{id}/progress`.

**Nodes** (`app/routers/nodes.py`)
- `POST /roadmaps/{id}/nodes`, `PATCH .../nodes/{node_id}`, `DELETE .../nodes/{node_id}`,
  `POST .../nodes/{node_id}/complete`, `POST .../nodes/{node_id}/uncomplete`,
  `PATCH .../nodes/reorder`.
- Deleting a node that other nodes depend on fails with **409** and an error message naming
  the blocking nodes. No cascade.

**Validation** (`app/validation.py`)
- `POST /roadmaps/{id}/validate` returns a report of problems (empty list when clean).
- The same check runs automatically inside every node create, edit and delete. The write is
  flushed, validated, and **rolled back with a 400** if it introduced a cycle, a self-reference,
  a dangling reference, or a reference to a node in another roadmap.
- Problem types: `cycle`, `self_reference`, `dangling_reference`, `foreign_reference`.

**Dashboard** — `GET /dashboard`: every roadmap of the current user with title, status and
progress percentage.

**Ownership scoping** — all roadmap and node routes resolve through `get_owned_roadmap` /
`get_owned_node` (`app/deps.py`), which filter on `user_id`. Another user's roadmap is
indistinguishable from a nonexistent one.

**Data model** (`app/models.py`) — `users`, `roadmaps`, `nodes`, plus `node_dependencies`, a
real self-referential association table (`depends_on_node_id`, `dependent_node_id`) with
foreign keys in both directions. It is not exposed as its own resource; every node response
embeds `depends_on` as a flat list of node ids.

**Migrations** — Alembic, wired to `Base.metadata` and to the app settings
(`migrations/env.py`). `migrations/versions/0001_initial_schema.py` creates the full schema.

## Roadmap generation

`POST /roadmaps` commits the roadmap as `pending` and returns immediately; generation runs
afterwards in a FastAPI `BackgroundTask` (`run_generation`), which opens its own session via
`database.session_scope()` because the request-scoped one is closed by then. Clients poll
`GET /roadmaps/{id}/generation-status`. `POST /roadmaps/{id}/regenerate` clears the graph,
resets to `pending`, and schedules the same task.

The flow, using the `groq` SDK with model `llama-3.3-70b-versatile`:

1. **Classify** (status -> `generating_phases`). One call returning
   `{"type": "sequential" | "flat", "reasoning": ...}`. The classifier's answer overrides
   whatever `type` the client sent on create.
2. **Flat path** � one call returning independent `{name, description, time_estimate}`
   objects. Nodes get `order` by list position and no dependencies. Status -> `done`.
3. **Sequential path** � one call for 4-8 phases (name + one-line description only), then
   status -> `generating_tasks` and one call per phase for its atomic tasks. All nodes are
   created first, then the dependency edges are wired, then `validate_roadmap_graph` runs
   before the status becomes `done`.

Every call passes `response_format={"type": "json_schema", "json_schema": {...},
"strict": True}`, so responses are schema-conforming by construction and there is no
free-text parsing or repair loop.

### Prompt and schema design choices

- **Cross-phase dependencies are integer indices, not ids.** Each task carries
  `depends_on_previous_phase` (indices into the previous phase's task list) and
  `depends_on_earlier_in_phase` (indices of earlier tasks in the same phase). The model never
  invents an identifier, so a hallucination is an out-of-range integer that gets dropped with
  a log line, rather than a dangling edge that reaches the database. This is what gives
  task-level rather than phase-level ordering, as asked.
- **The graph is acyclic by construction.** Cross-phase edges only ever point backwards one
  phase, and within-phase edges are accepted only when the index is strictly less than the
  task's own. `validate_roadmap_graph` still runs before `done` as a backstop; if it ever
  finds a problem the graph is discarded and the roadmap goes to `failed`.
- **Phase prompts get adjacent-phase context**: the full phase list with the current one
  marked, the previous phase's tasks printed with their indices, and the next phase named so
  the model does not do its work early.
- **A task in phase N+1 with no stated dependency falls back to the previous phase's last
  task**, so no node floats free of the ordering in a roadmap that was classified sequential.
- **Strict mode requires `additionalProperties: false` and every property in `required`**, so
  schemas are built through a small `_obj()` helper that enforces both.
- **Model output is treated as untrusted**: names are truncated to the column width, missing
  names fall back to a placeholder, and index lists are coerced through `_as_indices`.

### Failure handling

Every Groq call is wrapped; API errors, timeouts, unparseable JSON, an unknown classification
value, and a post-generation validation failure all raise `GenerationError`. `run_generation`
catches everything, rolls back the partial graph, and records `status = "failed"` plus the
reason in a new **`error_message`** column on `Roadmap` (returned by both `GET /roadmaps/{id}`
and `GET /roadmaps/{id}/generation-status`). The exception is also logged with a traceback.
The background task never raises, so it cannot die silently.

**With no `GROQ_API_KEY` configured, `build_client()` returns `None` and generation is skipped**
� the roadmap is created and simply stays in `pending` with no nodes. `POST /roadmaps` still
succeeds, so the rest of the API is usable before a key is in place. This is also what keeps
the test suite off the network.

## Running locally

Prerequisites: `uv`, and a PostgreSQL instance.

```bash
uv sync                          # install dependencies into .venv
cp .env.example .env             # then edit it
```

Environment variables (all read by `app/config.py`, from `.env` or the real environment):

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://postgres:postgres@localhost:5432/roadmap` | The database must already exist: `createdb roadmap`. |
| `JWT_SECRET` | `dev-secret-change-me` | **Must** be replaced outside development. |
| `JWT_ALGORITHM` | `HS256` | |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` | 24 hours. |
| `GROQ_API_KEY` | *(empty)* | Roadmap generation is skipped while this is empty. A `.env` file with an empty `GROQ_API_KEY=` line is already at the project root; fill it in. |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | |
| `GROQ_TIMEOUT_SECONDS` | `60` | Per-request timeout passed to the SDK. |
| `GROQ_MAX_RETRIES` | `2` | SDK-level retries on transient errors. |

Apply migrations and start the server:

```bash
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

Interactive docs at http://127.0.0.1:8000/docs, health check at `/health`.

## Running the tests

```bash
uv run pytest
```

**73 tests, all passing.** They run against an in-memory SQLite database created per test from
`Base.metadata` (no Postgres or migrations needed), with `get_db` overridden by a fixture and
`database.SessionLocal` repointed at the same engine so background tasks reach it too.

Coverage: the register/login flow and token validation (missing, malformed, expired,
wrong-secret tokens), the bcrypt helpers directly, roadmap CRUD scoped to the correct user,
node CRUD, cycle rejection (two-node, longer, and self-referential), the 409 on deleting a
node that has dependents, and the full generation flow.

**No test touches the Groq API.** `conftest.py` hard-sets `GROQ_API_KEY=""` (not
`setdefault`), so a real key in the environment or `.env` cannot leak into a test run. The
generation tests monkeypatch `app.generation.build_client` to return a `FakeGroq` that replays
canned structured-output payloads keyed by json_schema name, and records every call so the
prompts and schemas can be asserted on. They cover: classification routing to the flat and
sequential paths, flat generation producing ordered nodes with no dependencies, sequential
generation producing a valid acyclic graph with the right task-level edges, out-of-range model
indices being dropped, strict json_schema being used on every call, a Groq API failure setting
`failed` without crashing the background task or leaving a partial graph, and regeneration
replacing an existing graph.

## Decisions not specified in the brief

- **Password hashing uses `bcrypt` directly, with no passlib.** passlib 1.7.4 is broken
  against modern bcrypt, so the dependency is gone rather than pinned around. `hash_password`
  is `bcrypt.hashpw(password.encode(), bcrypt.gensalt())`; `verify_password` is
  `bcrypt.checkpw`, returning `False` rather than raising on a malformed stored hash or an
  over-long candidate. Because bcrypt caps input at 72 **bytes**, registration validates the
  UTF-8 byte length as well as the character length, so a non-ASCII password is rejected with
  a 422 instead of blowing up in the hasher.
- **404, not 403, for another user's resources**, so the API does not leak which ids exist.
- **`time_estimate` is a free-form string** (`"2 hours"`, `"3 days"`), per the node shape in the
  spec. `description` and `time_estimate` are nullable; `name` is required.
- **Title is optional on create** and derived from `goal_text` when omitted (trimmed to 80
  characters with an ellipsis). `type` defaults to `sequential`.
- **`order` defaults to `max(order) + 1`** within the roadmap, so nodes append in creation order.
- **`progress_percentage` is a float rounded to 2 decimals**; an empty roadmap reports `0`.
  It is also returned inline on roadmap list, detail and dashboard responses, not only from
  `/roadmaps/{id}/progress`.
- **`GET /roadmaps/{id}/progress` returns `total_nodes` and `completed_nodes` too**, since a
  bare percentage is not enough to render a "3 of 8" style indicator.
- **`PATCH .../nodes/reorder` is declared before `PATCH .../nodes/{node_id}`** so `reorder` is
  not parsed as a node id. It takes `{"items": [{"node_id": ..., "order": ...}]}`, updates only
  the nodes listed, and 400s if any id is not in the roadmap.
- **`depends_on` on PATCH is a full replacement**, not a merge, and duplicate ids are deduplicated.
- **PATCH bodies use `exclude_unset`**, so omitted fields are left alone.
- **`POST /auth/logout` was not implemented.** The brief's endpoint list omits it, and with no
  token blocklist it would be a no-op; clients discard the token instead.
- **Validation errors return a structured `detail`** — `{"message": ..., "problems": [...]}` —
  so a client can point at the offending nodes rather than parse a sentence.
- **Emails are normalised to lowercase** on register and login.
- **Foreign keys are enforced on SQLite** (`PRAGMA foreign_keys=ON`) so test behaviour matches
  Postgres.

## Changelog

**Pass 2**

- **bcrypt fix.** passlib removed entirely (`uv remove passlib`) and `bcrypt` unpinned to 5.x.
  `app/security.py` now calls `bcrypt.hashpw` / `bcrypt.checkpw` directly. Register and login
  were genuinely broken under the old passlib + bcrypt 5 combination; `tests/test_password_hashing.py`
  covers the helpers directly plus a full register -> login -> `/auth/me` round trip.
- **Real generation.** The two stubs in `app/generation.py` are replaced by the Groq
  implementation described above, run in a background task. Added the `groq` dependency, the
  `GROQ_*` settings, a `.env` file with an empty `GROQ_API_KEY=` line (`.env` was already
  gitignored), the `roadmaps.error_message` column, and migration `0002`.
- **Postgres left unverified by request.** No connection was attempted and `alembic upgrade head`
  was not run. Both migrations were reviewed by reading. Tests remain SQLite-only.
