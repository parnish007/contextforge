# ContextForge — How to Use

> **Author:** Trilochan Sharma — Independent Researcher · [parnish007](https://github.com/parnish007)

This guide picks up after installation. It covers: verifying the system is ready, using it for multiple projects, switching between projects, understanding what gets stored where, and exporting or pushing your data.

---

## 1. Verify the System Is Ready

### Step 1 — Check dependencies

```bash
python -c "import mcp, loguru, cryptography; print('Core deps OK')"
```

If this fails: `pip install -r requirements.txt`.

### Step 2 — Initialize the database

```bash
python -c "
from src.core.storage import StorageAdapter
s = StorageAdapter('data/contextforge.db')
print('DB ready:', 'data/contextforge.db')
"
```

The file is created automatically on first use.

### Step 3 — Fast smoke test

```bash
python -X utf8 benchmark/test_v5/iter_01_core.py
```

Expected: `75/75 tests passed` in ~5 seconds.

### Step 4 — Verify MCP server starts

```bash
# Windows
python mcp/server.py --stdio
# Ctrl+C to stop — if it starts without an import error you're good
```

### Step 5 — Check your AI key (optional)

The system works fully offline with no API keys using Ollama or rule-based fallback. To verify Groq:

```bash
python -c "
import os, httpx
key = os.getenv('GROQ_API_KEY','')
if not key: print('GROQ_API_KEY not set — offline mode active'); exit()
r = httpx.get('https://api.groq.com/openai/v1/models', headers={'Authorization':f'Bearer {key}'}, timeout=5)
print('Groq status:', r.status_code)
"
```

---

## 2. Yes — One Instance, Multiple Projects

**ContextForge is designed for multiple projects.** You set it up once. All projects share the same running MCP server and the same SQLite database. Every piece of data (decisions, tasks, history) is isolated by `project_id` — a string you choose when you create the project.

```
One MCP server instance
└── data/contextforge.db
    ├── projects:       my-saas-app, research-paper, learning-rust, client-xyz
    ├── decision_nodes: (each row has project_id = one of the above)
    ├── tasks:          (each row has project_id = one of the above)
    └── historical_nodes: (archived/duplicate nodes, also project-scoped)
```

No configuration change is needed to add a new project. Just call `init_project` with a new `project_id`.

---

## 3. Project Lifecycle

### Create a project

In your IDE, ask the AI assistant:

```
Use init_project with:
  project_id   = "my-saas-app"
  name         = "My SaaS App"
  project_type = "code"
  description  = "Multi-tenant subscription service"
  goals        = ["Launch MVP by Q3", "100 paying users"]
```

`project_type` options: `code`, `research`, `study`, `general`, `custom`.

You can also create it from Python:

```python
from src.core.storage import StorageAdapter
storage = StorageAdapter("data/contextforge.db")
storage.upsert_project({
    "id":           "my-saas-app",
    "name":         "My SaaS App",
    "project_type": "code",
    "description":  "Multi-tenant subscription service",
    "goals":        ["Launch MVP by Q3"],
    "tech_stack":   {"backend": "FastAPI", "db": "Postgres"},
})
```

### List all your projects

```
Use list_projects.
```

Returns all registered projects with their type, description, and creation date. Use this to check what exists before switching.

From Python:

```python
from src.core.storage import StorageAdapter
projects = StorageAdapter("data/contextforge.db").list_projects()
for p in projects:
    print(p["id"], "-", p["name"], f"({p['project_type']})")
```

### Enter a project (load context)

"Entering" a project means loading its context at the start of a session. Ask:

```
Use load_context with project_id="my-saas-app" and detail_level="L2".
```

| Level | What you get |
|-------|-------------|
| `L0` | Project name, type, description, goals only |
| `L1` | L0 + decision titles and areas |
| `L2` | L1 + full rationale, alternatives, confidence for each decision |

Start each session with `L2` so the AI has full context about prior decisions.

### Work inside a project

Every tool that reads or writes project data takes `project_id`. Pass it explicitly:

```
Use capture_decision with:
  project_id = "my-saas-app"
  summary    = "Use Postgres over MongoDB"
  rationale  = "Need ACID guarantees for billing and subscriptions"
  area       = "database"
  alternatives = [{"name": "MongoDB", "rejected_because": "No ACID across collections"}]

Use get_knowledge_node with query="database" project_id="my-saas-app"

Use search_context with query="Postgres schema"    (searches ContextForge's local files)
```

### Switch to a different project

There is no "exit" or "switch" command — switching is instant. Just use a different `project_id`:

```
Use load_context with project_id="research-paper" and detail_level="L2".
```

The previous project's data is untouched. You are now working in `research-paper`. Switch back at any time:

```
Use load_context with project_id="my-saas-app" and detail_level="L1".
```

### Work on multiple projects in the same conversation

Since every call is stateless and scoped by `project_id`, you can reference multiple projects in the same session without any special setup:

```
Use get_knowledge_node with query="auth" project_id="my-saas-app"
Use get_knowledge_node with query="auth" project_id="client-xyz"
```

---

## 4. What Is and Isn't Project-Scoped

This is important to understand before relying on isolation.

| Feature | Project-scoped? | Notes |
|---------|:--------------:|-------|
| Decision nodes | ✅ Yes | Fully isolated by `project_id` |
| Tasks | ✅ Yes | Fully isolated by `project_id` |
| Historical nodes | ✅ Yes | Archived duplicates, scoped by `project_id` |
| Project metadata | ✅ Yes | Name, goals, tech stack |
| Event ledger | ❌ **No** | All events go to one shared ledger. Rollback affects the whole ledger, not a single project |
| `search_context` | ❌ **No** | Searches ContextForge's own source tree (`src/`, `mcp/`, etc.), not your project's files. Use `get_knowledge_node` and `load_context` for project-specific retrieval |
| Snapshots | ❌ **No** | A `.forge` snapshot captures the entire ledger — all projects |

**Practical implication for rollback:** if you call `rollback` with an `event_id`, it marks that event and all newer events as inactive across all projects in the ledger. Use rollback carefully when running multiple projects in the same database.

**Practical implication for search:** `search_context` is useful for understanding the ContextForge codebase itself (e.g., finding relevant source files when building on top of it). For your own project's documentation and code, use `capture_decision` to store decisions, then `get_knowledge_node` or `load_context` to retrieve them.

---

## 5. Capture Decisions — The Main Workflow

The core value of ContextForge is accumulating project decisions over time so the AI never loses context. Every significant decision should be captured:

```
Use capture_decision with:
  project_id   = "my-saas-app"
  summary      = "JWT RS256 for API auth, not HS256"
  rationale    = "RS256 lets us verify tokens in services without sharing a secret key"
  area         = "auth"
  alternatives = [
    {"name": "HS256", "rejected_because": "Requires sharing secret across services"}
  ]
  confidence   = 0.9
  file_refs    = ["src/auth/tokens.py", "src/middleware/verify.py"]
```

Over time, `load_context` with `detail_level="L2"` assembles all of these into a complete decision history the AI can reason over.

---

## 6. Time-Travel Rollback

Rollback reverts the event ledger to a prior state. Use it when you want to undo a bad capture.

**Step 1 — Find the event to roll back to:**

```
Use list_events with last_n=20.
```

Note the `event_id` of the last good event (the one *before* the bad write).

**Step 2 — Roll back:**

```
Use rollback with event_id="<the event_id>".
```

Events are never deleted — they are marked `rolled_back` in the ledger. The node in `decision_nodes` is also soft-deleted (status = `inactive`). You can re-capture the decision correctly afterward.

**Remember:** rollback is ledger-wide, not project-specific.

---

## 7. Snapshots — Backup and Cross-Device

### Create a snapshot

```
Use snapshot with label="before-auth-refactor".
```

Creates an AES-256-GCM encrypted `.forge` file in the `.forge/` directory. Captures the entire ledger (all projects).

### Restore on another machine

Copy the `.forge` file to the other machine, then:

```
Use replay_sync with forge_path=".forge/before-auth-refactor.forge".
```

This replays all events from the snapshot onto the ledger at the new location. Set the same `FORGE_SNAPSHOT_KEY` in both machines' `.env` files.

### Automatic snapshots

FluidSync auto-checkpoints every 15 minutes while the server is running. These appear in `.forge/` with timestamps. The interval is configurable via `IDLE_MINUTES` in `.env`.

---

## 8. Where Your Data Lives

| What | Location | Format |
|------|----------|--------|
| All decisions and events | `data/contextforge.db` | SQLite (3 tables + indexes) |
| Encrypted snapshots | `.forge/` | AES-256-GCM binary |
| Benchmark results (JSON) | `data/academic_metrics.json` | Machine-readable |
| Benchmark results (human) | `data/academic_metrics.md` | Markdown |
| Publication charts | `docs/assets/` | PNG at 300 DPI |

### Inspect the database directly

```bash
sqlite3 data/contextforge.db

# All projects
SELECT id, name, project_type, created_at FROM projects;

# All decisions for a project
SELECT id, area, summary, confidence FROM decision_nodes
WHERE project_id='my-saas-app' AND status='active';

# Recent ledger events
SELECT event_id, event_type, status, created_at FROM events
ORDER BY rowid DESC LIMIT 20;

# Check if ReviewerGuard blocked anything
SELECT COUNT(*) FROM events WHERE event_type='CONFLICT';

# Active node count per project
SELECT project_id, COUNT(*) as nodes FROM decision_nodes
WHERE status='active' GROUP BY project_id;
```

---

## 9. Export and Push Your Data

### Export all decisions for a project as JSON

```python
from src.core.storage import StorageAdapter
import json, pathlib

storage = StorageAdapter("data/contextforge.db")
nodes = storage.list_nodes(project_id="my-saas-app", status="active")

pathlib.Path("export").mkdir(exist_ok=True)
pathlib.Path("export/my-saas-app-decisions.json").write_text(
    json.dumps(nodes, indent=2, default=str)
)
print(f"Exported {len(nodes)} decisions")
```

### Export the entire event ledger

```python
from src.memory.ledger import EventLedger
import json, pathlib

events = EventLedger("data/contextforge.db").list_events(last_n=100_000)
pathlib.Path("export/ledger.json").write_text(
    json.dumps(events, indent=2, default=str)
)
```

### Export all projects and their decisions

```python
from src.core.storage import StorageAdapter
import json, pathlib

storage = StorageAdapter("data/contextforge.db")
output = {}
for project in storage.list_projects():
    pid = project["id"]
    output[pid] = {
        "meta":  project,
        "nodes": storage.list_nodes(project_id=pid, status="active"),
    }

pathlib.Path("export/all-projects.json").write_text(
    json.dumps(output, indent=2, default=str)
)
print(f"Exported {len(output)} projects")
```

### Copy the database to another machine

The `.db` file is fully self-contained. Copy it anywhere SQLite is available:

```bash
cp data/contextforge.db ~/Backups/contextforge-$(date +%Y%m%d).db

# On new machine — point the server at the copy
DB_PATH=/path/to/copied.db python mcp/server.py --stdio
```

### Push to Postgres

```python
import psycopg2, json
from src.core.storage import StorageAdapter

storage = StorageAdapter("data/contextforge.db")
conn = psycopg2.connect("postgresql://user:pass@host/db")
cur = conn.cursor()

for node in storage.list_nodes("my-saas-app", status="active"):
    cur.execute("""
        INSERT INTO knowledge_nodes
            (id, project_id, area, summary, rationale, confidence, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
    """, (
        node["id"], node["project_id"], node["area"],
        node["summary"], node.get("rationale",""),
        node.get("confidence", 0.5), node.get("created_at"),
    ))

conn.commit()
conn.close()
```

### Push to a REST API

```python
import httpx, json
from src.core.storage import StorageAdapter

nodes = StorageAdapter("data/contextforge.db").list_nodes("my-saas-app")
httpx.post(
    "https://your-api.example.com/import",
    json={"project_id": "my-saas-app", "nodes": nodes},
    timeout=30,
)
```

---

## 10. Quick Reference — All MCP Tools

| Tool | Required params | What it does |
|------|----------------|-------------|
| `list_projects` | — | List all registered projects |
| `init_project` | `project_id`, `name` | Create or update a project |
| `load_context` | `project_id` | Load L0/L1/L2 context for a project |
| `get_knowledge_node` | `query` | Search decisions by keyword |
| `capture_decision` | `project_id`, `summary`, `area` | Store a decision with rationale |
| `search_context` | `query` | Search ContextForge source files locally |
| `rollback` | `event_id` or `timestamp` | Revert ledger to a prior state (ledger-wide) |
| `snapshot` | — | Create AES-256-GCM encrypted backup |
| `replay_sync` | `forge_path` | Restore from a `.forge` snapshot |
| `list_events` | — | Inspect the append-only event ledger |

---

## 11. What ContextForge Does NOT Have (Honest Gaps)

| Feature | Status | Workaround |
|---------|--------|-----------|
| Delete a project | ❌ Not implemented | Set all its nodes to `status='archived'` directly in SQLite |
| Per-project rollback | ❌ Rollback is ledger-wide | Snapshot before risky operations; restore if needed |
| Search your own project's files | ❌ `search_context` scans ContextForge source only | Use `capture_decision` to store knowledge; retrieve via `get_knowledge_node` |
| Active project session state | ❌ No session — pass `project_id` every call | Use a system prompt that always includes your active `project_id` |
| Project rename | ❌ Not implemented | `upsert_project` with the same `id` and a new `name` — this updates the name |
| Project-to-project links | ❌ Not implemented | Reference another project's `project_id` in a decision's rationale text |

---

*ContextForge Nexus Architecture — reproducible, information-theoretically grounded agentic memory.*
