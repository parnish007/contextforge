# ContextForge — How to Use

> **Author:** Trilochan Sharma — Independent Researcher · [parnish007](https://github.com/parnish007)

← [README](../README.md) · [Setup](SETUP.md) · [What is this?](WHAT_IS_THIS.md) · [Architecture](ARCHITECTURE.md) · [Engineering Reference](ENGINEERING_REFERENCE.md)

Complete guide for using ContextForge after installation: verifying readiness, managing multiple projects, using all 22 MCP tools, understanding what is stored where, and exporting your data.

---

## Table of Contents

1. [Verify the System Is Ready](#1-verify-the-system-is-ready)
2. [One Instance, Multiple Projects](#2-one-instance-multiple-projects)
3. [Project Lifecycle — Create, Enter, Switch, Exit](#3-project-lifecycle)
4. [Decision Graph — Capture and Retrieve](#4-decision-graph)
5. [Task Management](#5-task-management)
6. [Ledger Operations — Rollback, Snapshot, Replay](#6-ledger-operations)
7. [What Is and Is Not Project-Scoped](#7-scoping-reference)
8. [Where Your Data Lives](#8-where-your-data-lives)
9. [Export and Push Data](#9-export-and-push-data)
10. [All 22 MCP Tools — Complete Reference](#10-complete-tool-reference)
11. [Honest Gaps](#11-honest-gaps)

---

## 1. Verify the System Is Ready

### Dependencies

```bash
python -c "import mcp, loguru, cryptography; print('Core deps OK')"
```

Fail → `pip install -r requirements.txt`

### Database

```bash
python -c "
from src.core.storage import StorageAdapter
s = StorageAdapter('data/contextforge.db')
print('DB ready')
"
```

### Smoke test (5 seconds)

```bash
python -X utf8 benchmark/test_v5/iter_01_core.py
# Expected: 75/75 tests passed
```

### MCP server starts

```bash
# Should start without import errors — Ctrl+C to stop
python mcp/server.py --stdio
```

### Optional — check your LLM key

The system falls back to rule-based processing if no LLM is available.

```bash
# Groq
python -c "
import os, httpx
key = os.getenv('GROQ_API_KEY','')
if not key: print('Not set — offline mode active'); exit()
r = httpx.get('https://api.groq.com/openai/v1/models',
              headers={'Authorization':f'Bearer {key}'}, timeout=5)
print('Groq:', r.status_code)
"

# Ollama
curl http://localhost:11434/api/tags
```

---

## 2. One Instance, Multiple Projects

**ContextForge is multi-project by design.** Set it up once. Add projects freely.

All data is isolated by `project_id` inside a single SQLite database:

```
data/contextforge.db
├── projects          — my-saas-app, research-paper, learning-rust, client-xyz, ...
├── decision_nodes    — every row has a project_id column
├── tasks             — every row has a project_id column
└── historical_nodes  — archived/duplicate nodes, also project-scoped
```

One MCP server serves all projects simultaneously. Switching between them takes a single tool call — no restart required.

```mermaid
graph LR
    MCP["ContextForge MCP Server"]
    P1["project: my-saas-app"]
    P2["project: research-paper"]
    P3["project: client-xyz"]
    DB[("data/contextforge.db")]

    MCP --> P1
    MCP --> P2
    MCP --> P3
    P1 & P2 & P3 --> DB
```

---

## 3. Project Lifecycle

### Create a project

```
Use init_project with:
  project_id   = "my-saas-app"
  name         = "My SaaS App"
  project_type = "code"
  description  = "Multi-tenant subscription service with Stripe"
  goals        = ["Launch MVP by Q3", "100 paying users in 6 months"]
  tech_stack   = {"backend": "FastAPI", "db": "Postgres", "auth": "JWT"}
  constraints  = ["No vendor lock-in", "GDPR compliant"]
```

`project_id` is your permanent slug — choose it carefully. `name` can be changed later with `rename_project`.

`project_type` options:

| Type | Use for |
|------|---------|
| `code` | Software projects, APIs, services |
| `research` | Papers, experiments, literature reviews |
| `study` | Learning, courses, skill building |
| `general` | Anything else |
| `custom` | Multi-discipline projects |

### List all projects

```
Use list_projects.
```

Returns all projects with type, description, and creation date. Always do this at the start of a session if you manage multiple projects.

### Enter a project (load context)

Loading context is how you "enter" a project — it fetches everything the AI needs:

```
Use load_context with project_id="my-saas-app" and detail_level="L2".
```

| Level | Contents | Use when |
|-------|----------|----------|
| `L0` | Name, type, description, goals, constraints, tech stack | Quick orientation |
| `L1` | L0 + decision titles and areas | Planning session |
| `L2` | L1 + full rationale, alternatives, file refs | Working session — use this |

You can also filter by area or keyword:

```
Use load_context with project_id="my-saas-app" detail_level="L2" area="auth"
Use load_context with project_id="my-saas-app" detail_level="L2" query="JWT"
```

### Switch between projects

There is no "exit" command — switching is instant. Just use a different `project_id`:

```
# Working in project A
Use load_context with project_id="my-saas-app" and detail_level="L2".

# Switch to project B — one call
Use load_context with project_id="research-paper" and detail_level="L2".

# Switch back
Use load_context with project_id="my-saas-app" and detail_level="L1".
```

Data in both projects is untouched during switches. You can even reference both in the same conversation.

### Rename a project

The `project_id` slug is permanent. Only the display name changes:

```
Use rename_project with:
  project_id      = "my-saas-app"
  new_name        = "AcmeSaaS — Production"
  new_description = "Now serving 50+ customers"
```

Omit `new_description` to keep the existing one.

### Update project metadata

`init_project` is idempotent — calling it again with the same `project_id` updates the metadata without touching any decisions or tasks:

```
Use init_project with:
  project_id = "my-saas-app"
  name       = "AcmeSaaS"
  goals      = ["Launch MVP", "100 paying users", "SOC 2 compliance"]
  tech_stack = {"backend": "FastAPI", "db": "Postgres", "queue": "Redis"}
```

### Get project statistics

```
Use project_stats with project_id="my-saas-app".
```

Returns:
- Node counts: total, active, deprecated, archived in historical
- Breakdown by area (auth: 12, database: 5, api-design: 8, ...)
- Task counts: pending, in_progress, done, blocked + completion %

### Merge two projects

When two projects should become one (e.g., a spike becomes part of main project):

```
Use merge_projects with:
  source_project_id = "saas-spike"
  target_project_id = "my-saas-app"
```

All decisions, tasks, and historical nodes from `saas-spike` are moved to `my-saas-app`. The source project is then deleted. **Irreversible — create a snapshot first.**

### Delete a project

```
Use delete_project with project_id="old-project"
```

By default, all active decision nodes are archived to `historical_nodes` before deletion. To skip archiving (faster but loses nodes permanently):

```
Use delete_project with project_id="old-project" archive_nodes=false
```

Tasks are permanently deleted with the project. **Irreversible — snapshot first.**

---

## 4. Decision Graph

### Capture a decision

The core workflow. Every architectural, design, or research decision should be captured:

```
Use capture_decision with:
  project_id   = "my-saas-app"
  summary      = "Use RS256 JWT, not HS256, for API authentication"
  rationale    = "RS256 allows token verification in downstream services without
                  sharing a secret key. Essential for our microservices architecture."
  area         = "auth"
  alternatives = [
    {"name": "HS256", "rejected_because": "Requires sharing secret across all services — security risk"},
    {"name": "Session cookies", "rejected_because": "Stateful — doesn't work across service boundaries"}
  ]
  confidence   = 0.92
  file_refs    = ["src/auth/tokens.py", "src/middleware/verify.py"]
```

The `area` field is free text but consistency matters — use the same area string across related decisions (e.g. always `auth`, never `authentication` in one and `auth` in another).

Common areas: `auth`, `database`, `api-design`, `infrastructure`, `security`, `performance`, `testing`, `deployment`, `data-model`, `ui`, `tooling`.

### Search decisions by keyword

```
Use get_knowledge_node with query="JWT" project_id="my-saas-app"
Use get_knowledge_node with query="database choice" project_id="my-saas-app" top_k=3
```

Searches across `summary`, `rationale`, and `area` fields.

### Browse decisions by area

```
Use list_decisions with project_id="my-saas-app" area="auth"
Use list_decisions with project_id="my-saas-app" status="deprecated" limit=50
```

`status` options: `active` (default), `deprecated`, `quarantined`, `pending`.

### Update a decision

When the rationale or confidence changes — without creating a new node:

```
Use update_decision with:
  node_id    = "abc123..."
  rationale  = "Updated: RS256 also lets us rotate keys without redeploying all services"
  confidence = 0.95
```

Allowed fields: `summary`, `rationale`, `area`, `confidence`, `importance`.

### Deprecate a decision

When a decision has been superseded by a newer one:

```
# Step 1 — capture the new decision
Use capture_decision with summary="Switch from JWT to Paseto for auth tokens" ...
# → returns node_id: "xyz789..."

# Step 2 — deprecate the old one and link to the replacement
Use deprecate_decision with:
  node_id        = "abc123..."
  reason         = "Paseto is simpler and avoids JWT algorithm confusion attacks"
  replacement_id = "xyz789..."
```

Deprecated nodes remain in the database and are visible with `list_decisions status=deprecated` — they are not deleted.

### Link decisions together

Create typed edges between related decisions:

```
Use link_decisions with:
  source_id = "abc123..."
  target_id = "def456..."
  edge_type = "depends_on"
```

Edge types and their meaning:

| Type | Meaning |
|------|---------|
| `depends_on` | Source decision requires target to be in place |
| `replaces` | Source supersedes target |
| `contradicts` | Source and target are in tension — documents the conflict |
| `refines` | Source adds detail or narrows scope of target |
| `implements` | Source is the concrete realization of target's direction |

---

## 5. Task Management

### Create tasks

```
Use create_task with:
  project_id  = "my-saas-app"
  title       = "Implement JWT refresh token rotation"
  description = "Replace simple expiry with sliding window rotation to prevent token theft"
  priority    = 2
  sprint      = "2026-Q2"
  assigned_to = "backend-team"
```

Priority: 1 = highest urgency, 5 = lowest.

### List tasks

```
Use list_tasks with project_id="my-saas-app"
Use list_tasks with project_id="my-saas-app" status="in_progress"
Use list_tasks with project_id="my-saas-app" status="blocked" limit=5
```

Status options: `pending`, `in_progress`, `done`, `blocked`.

### Update task status

```
Use update_task with task_id="tid123..." status="in_progress"
Use update_task with task_id="tid123..." status="done"
```

### Track progress

```
Use project_stats with project_id="my-saas-app"
# Returns: {"tasks": {"total": 12, "done": 8, "in_progress": 2, "blocked": 1, "pct_complete": 67}}
```

---

## 6. Ledger Operations

### Inspect the event log

Every write to the system creates an immutable event in the ledger:

```
Use list_events with last_n=20
Use list_events with event_type="CONFLICT" last_n=50
```

Events have types: `AGENT_THOUGHT` (decision writes), `CONFLICT` (blocked by ReviewerGuard), `ROLLBACK`, `SNAPSHOT`, etc.

A non-zero `CONFLICT` count means the entropy gate or charter check blocked a write — this is the security system working correctly.

### Time-travel rollback

When a bad decision was captured and you want to undo it:

```
# Step 1 — find the last good event
Use list_events with last_n=30
# Note the event_id of the last good event (the one BEFORE the bad write)

# Step 2 — roll back to just before it
Use rollback with event_id="evt_abc123..."
```

You can also roll back to a timestamp:

```
Use rollback with timestamp="2026-04-10T14:30:00Z"
```

**Important:** Rollback is ledger-wide — it affects all projects in the database. Events are marked `rolled_back`, not deleted. The corresponding `decision_node` is also soft-deleted (status → inactive). You can re-capture the correct version afterward.

### Create an encrypted snapshot

Before risky operations (merge, delete, major refactor):

```
Use snapshot with label="before-auth-refactor"
```

Creates an AES-256-GCM encrypted `.forge` file. Requires `FORGE_SNAPSHOT_KEY` in `.env` (minimum 32 characters).

### List existing snapshots

```
Use list_snapshots
```

Returns all `.forge` files with labels, sizes, and creation timestamps.

### Restore from a snapshot

On any machine with the same `FORGE_SNAPSHOT_KEY`:

```
Use replay_sync with forge_path=".forge/before-auth-refactor.forge"
```

Replays all events from the snapshot onto the current ledger.

### Automatic snapshots

FluidSync auto-checkpoints every 15 minutes while the server is running. These appear in `.forge/` with timestamp filenames. Configure the interval via `IDLE_MINUTES` in `.env`.

---

## 7. Scoping Reference

This is the most important thing to understand about multi-project usage:

| Feature | Project-scoped? | Notes |
|---------|:--------------:|-------|
| Decision nodes | ✅ Yes | Fully isolated by `project_id` |
| Tasks | ✅ Yes | Fully isolated by `project_id` |
| Historical nodes | ✅ Yes | Archived duplicates, per-project |
| Project metadata | ✅ Yes | Name, type, goals, tech stack |
| `load_context` | ✅ Yes | Returns only the specified project's data |
| `list_decisions` | ✅ Yes | Filtered by `project_id` |
| Event ledger | ❌ No | Shared across all projects |
| `rollback` | ❌ No | Ledger-wide, affects all projects |
| `snapshot` / `replay_sync` | ❌ No | Captures entire database |
| `search_context` | ❌ No | Searches ContextForge source files, not your project |

**For retrieval of your project's knowledge**, always use `get_knowledge_node`, `load_context`, or `list_decisions` — these are project-scoped.

**`search_context`** searches `src/`, `mcp/`, and `prompts/` in the ContextForge installation directory. It is useful when building on top of ContextForge, not for searching your own project's files.

---

## 8. Where Your Data Lives

| What | Location | Format |
|------|----------|--------|
| All decisions, tasks, events | `data/contextforge.db` | SQLite — WAL mode |
| Encrypted snapshots | `.forge/` | AES-256-GCM binary |
| Benchmark metrics (JSON) | `data/academic_metrics.json` | Machine-readable |
| Benchmark metrics (readable) | `data/academic_metrics.md` | Markdown |
| Publication charts | `docs/assets/` | PNG, 300 DPI |

### Inspect directly via SQLite

```bash
sqlite3 data/contextforge.db

# All projects
SELECT id, name, project_type, created_at FROM projects;

# Decisions for a project, by area
SELECT area, summary, confidence FROM decision_nodes
WHERE project_id='my-saas-app' AND status='active'
ORDER BY area, importance DESC;

# How many decisions per area
SELECT area, COUNT(*) FROM decision_nodes
WHERE project_id='my-saas-app' AND status='active'
GROUP BY area ORDER BY COUNT(*) DESC;

# Tasks by status
SELECT status, COUNT(*) FROM tasks
WHERE project_id='my-saas-app' GROUP BY status;

# Recent ledger events
SELECT event_id, event_type, status, created_at
FROM events ORDER BY rowid DESC LIMIT 20;

# Events blocked by ReviewerGuard
SELECT COUNT(*) FROM events WHERE event_type='CONFLICT';

# Deprecated decisions
SELECT id, area, summary, deprecated_reason
FROM decision_nodes WHERE status='deprecated';
```

---

## 9. Export and Push Data

### Export all decisions for a project

```python
from src.core.storage import StorageAdapter
import json, pathlib

storage = StorageAdapter("data/contextforge.db")
nodes = storage.list_nodes(project_id="my-saas-app", status="active", limit=10000)

pathlib.Path("export").mkdir(exist_ok=True)
pathlib.Path("export/decisions.json").write_text(
    json.dumps(nodes, indent=2, default=str)
)
print(f"Exported {len(nodes)} decisions")
```

### Export all projects

```python
from src.core.storage import StorageAdapter
import json, pathlib

storage = StorageAdapter("data/contextforge.db")
output = {}
for p in storage.list_projects():
    pid = p["id"]
    output[pid] = {
        "meta":     p,
        "nodes":    storage.list_nodes(pid, status="active", limit=10000),
        "tasks":    storage.list_tasks(pid, limit=1000),
        "stats":    storage.get_project_stats(pid),
    }

pathlib.Path("export/all_projects.json").write_text(
    json.dumps(output, indent=2, default=str)
)
print(f"Exported {len(output)} projects")
```

### Copy database to another machine

The `.db` file is fully self-contained:

```bash
cp data/contextforge.db ~/Backups/contextforge-$(date +%Y%m%d).db

# On the new machine
DB_PATH=/path/to/copied.db python mcp/server.py --stdio
```

### Push to Postgres

```python
import psycopg2, json
from src.core.storage import StorageAdapter

s = StorageAdapter("data/contextforge.db")
conn = psycopg2.connect("postgresql://user:pass@host/db")
cur = conn.cursor()

for node in s.list_nodes("my-saas-app", status="active", limit=10000):
    cur.execute(
        """INSERT INTO knowledge_nodes
           (id, project_id, area, summary, rationale, confidence, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
        (node["id"], node["project_id"], node["area"],
         node["summary"], node.get("rationale",""),
         node.get("confidence", 0.5), node.get("created_at")),
    )

conn.commit()
conn.close()
```

### Push to a REST API

```python
import httpx
from src.core.storage import StorageAdapter

nodes = StorageAdapter("data/contextforge.db").list_nodes("my-saas-app", limit=10000)
httpx.post(
    "https://your-api.example.com/import",
    json={"project_id": "my-saas-app", "nodes": nodes},
    timeout=30,
)
```

---

## 10. Complete Tool Reference (22 tools)

### Project management

| Tool | Required params | Optional params | What it does |
|------|----------------|----------------|-------------|
| `list_projects` | — | — | List all registered projects |
| `init_project` | `project_id`, `name` | `project_type`, `description`, `goals`, `tech_stack`, `constraints` | Create or update project |
| `rename_project` | `project_id`, `new_name` | `new_description` | Rename display name |
| `merge_projects` | `source_project_id`, `target_project_id` | — | Merge source INTO target, delete source |
| `delete_project` | `project_id` | `archive_nodes` (default true) | Permanently delete project |
| `project_stats` | `project_id` | — | Node/task/area summary |

### Decision graph

| Tool | Required params | Optional params | What it does |
|------|----------------|----------------|-------------|
| `capture_decision` | `project_id`, `summary`, `area` | `rationale`, `alternatives`, `confidence`, `file_refs` | Store a decision |
| `load_context` | `project_id` | `detail_level`, `query`, `area`, `top_k` | Load L0/L1/L2 context |
| `get_knowledge_node` | `query`, `project_id` | `top_k` | Keyword search decisions |
| `list_decisions` | `project_id` | `area`, `status`, `limit` | Browse decisions |
| `update_decision` | `node_id` + at least one field | `summary`, `rationale`, `area`, `confidence`, `importance` | Edit a decision |
| `deprecate_decision` | `node_id`, `reason` | `replacement_id` | Mark as superseded |
| `link_decisions` | `source_id`, `target_id`, `edge_type` | — | Create typed relationship |

### Tasks

| Tool | Required params | Optional params | What it does |
|------|----------------|----------------|-------------|
| `list_tasks` | `project_id` | `status`, `limit` | List tasks |
| `create_task` | `project_id`, `title` | `description`, `priority`, `sprint`, `assigned_to` | Create task |
| `update_task` | `task_id`, `status` | — | Update task status |

### Ledger & sync

| Tool | Required params | Optional params | What it does |
|------|----------------|----------------|-------------|
| `rollback` | `event_id` or `timestamp` | — | Revert ledger (ledger-wide) |
| `snapshot` | — | `label` | Encrypted checkpoint |
| `list_snapshots` | — | — | List `.forge` files |
| `replay_sync` | `forge_path` | — | Restore from snapshot |
| `list_events` | — | `last_n`, `event_type` | Inspect event log |

### Search

| Tool | Required params | Optional params | What it does |
|------|----------------|----------------|-------------|
| `search_context` | `query` | `top_k`, `threshold` | Search ContextForge source files |

---

## 11. Honest Gaps

Things that are not currently implemented:

| Feature | Status | Best workaround |
|---------|--------|----------------|
| Per-project rollback | Not implemented | `snapshot` before risky ops; `replay_sync` to restore |
| Search your own project's files | Not implemented | Capture knowledge as decisions; retrieve with `get_knowledge_node` |
| Duplicate a project | Not implemented | Export JSON → `init_project` new id → import nodes via Python API |
| Project-to-project links | Not implemented | Reference the other project's `project_id` in decision rationale text |
| Bulk import decisions | Not implemented | Use `StorageAdapter.upsert_node()` in a Python loop |
| Granular access control | Not implemented | All projects share one server — separate DBs for separate users |

---

*ContextForge Nexus Architecture — reproducible, information-theoretically grounded agentic memory.*
