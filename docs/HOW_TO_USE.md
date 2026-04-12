# ContextForge — How to Use

> **Author:** Trilochan Sharma — Independent Researcher · [parnish007](https://github.com/parnish007)

This guide picks up after installation. It walks through verifying the system is ready, using it as an MCP tool in your IDE, running the benchmarks, and finding your data when you need it later.

---

## 1. Verify the System Is Ready

### Step 1 — Check dependencies

```bash
python -c "import mcp, loguru, cryptography; print('Core deps OK')"
```

If this fails, run `pip install -r requirements.txt`.

### Step 2 — Initialize the database

The SQLite database is created automatically on first use. You can verify it exists and is writable:

```bash
python -c "
from src.core.storage import StorageAdapter
s = StorageAdapter('data/contextforge.db')
print('DB ready at data/contextforge.db')
"
```

You should see `DB ready at data/contextforge.db`. The file will be created if it does not yet exist.

### Step 3 — Run the fast smoke test

```bash
python -X utf8 benchmark/test_v5/iter_01_core.py
```

Expected output: `75/75 tests passed` in about 5 seconds. If any tests fail, the error message will point to the broken component.

### Step 4 — Verify the MCP server starts

```bash
python mcp/server.py --stdio &
sleep 1 && kill %1 && echo "MCP server OK"
```

If you see an import error, install the missing package (`pip install mcp`).

### Step 5 (optional) — Verify your LLM backend

**Groq:**
```bash
python -c "
import os, httpx
key = os.getenv('GROQ_API_KEY','')
if not key: print('GROQ_API_KEY not set'); exit()
r = httpx.get('https://api.groq.com/openai/v1/models', headers={'Authorization':f'Bearer {key}'}, timeout=5)
print('Groq:', r.status_code)
"
```

**Ollama:**
```bash
curl http://localhost:11434/api/tags
```

If neither works, the system falls back to rule-based processing automatically — you will still get memory, rollback, and search tools.

---

## 2. Use ContextForge as an MCP Tool in Your IDE

### Connect your IDE

Follow [`docs/SETUP.md`](SETUP.md) for your IDE. The minimum config for Claude Desktop:

```json
{
  "mcpServers": {
    "contextforge": {
      "command": "python",
      "args": ["mcp/server.py", "--stdio"],
      "cwd": "C:/Users/YourName/Projects/contextforge",
      "env": { "DB_PATH": "data/contextforge.db" }
    }
  }
}
```

Restart the IDE after saving. You should see `contextforge` in the tool list.

### First conversation — set up a project

Ask your AI assistant:

```
Use init_project with project_id="my-project" and name="My Project".
```

Then load context to confirm it worked:

```
Use load_context with project_id="my-project" and detail_level="L0".
```

You should see the project metadata returned as JSON.

### Capture a decision

```
Use capture_decision with:
  project_id = "my-project"
  summary    = "Use SQLite for local storage"
  rationale  = "Zero infrastructure overhead, WAL mode supports concurrent reads"
  area       = "database"
```

The tool will return an `event_id` and `node_id`. Both are permanent — the event is appended to the ledger, the node goes into the knowledge graph.

### Query the knowledge graph

```
Use get_knowledge_node with query="database" and project_id="my-project".
```

Returns matching nodes with their full rationale. The query is a keyword match against summary, rationale, and area fields.

### Search your local files

```
Use search_context with query="JWT authentication".
```

This runs local-edge cosine similarity (no cloud tokens). Returns the most relevant file chunks above the θ = 0.75 threshold.

### Load rich context before working

At the start of any session, load full context so the AI has everything it needs:

```
Use load_context with project_id="my-project" and detail_level="L2".
```

`L0` = project summary only. `L1` = + decision titles. `L2` = + full rationale and alternatives.

### Time-travel rollback

If something went wrong, roll back the ledger to a prior event:

```
Use list_events with last_n=10.
```

Find the `event_id` before the bad write. Then:

```
Use rollback with event_id="<the id>".
```

Events are never deleted — rollback marks them as inactive. You can roll forward again by re-capturing decisions.

### Encrypted snapshot (backup)

Before a major refactor:

```
Use snapshot with label="before-auth-refactor".
```

Creates an AES-256-GCM encrypted `.forge` file in the `.forge/` directory. To restore on another machine:

```
Use replay_sync with forge_path=".forge/before-auth-refactor.forge".
```

---

## 3. Where Your Data Lives

| What | Location | Format |
|------|----------|--------|
| All decisions and events | `data/contextforge.db` | SQLite, 3 tables |
| Encrypted snapshots | `.forge/` | AES-256-GCM binary |
| Benchmark results | `data/academic_metrics.json` | JSON |
| Benchmark results (readable) | `data/academic_metrics.md` | Markdown |
| Publication charts | `docs/assets/` | PNG, 300 DPI |

### Inspecting the database directly

```bash
# Open the SQLite database
sqlite3 data/contextforge.db

# Show all tables
.tables

# See all active knowledge nodes
SELECT id, project_id, area, summary, confidence FROM decision_nodes WHERE status='active';

# See recent events
SELECT id, event_type, timestamp FROM events ORDER BY rowid DESC LIMIT 20;

# Full text of a specific node
SELECT * FROM decision_nodes WHERE id='<node_id>';
```

### Export all decisions as JSON

```python
from src.core.storage import StorageAdapter

storage = StorageAdapter("data/contextforge.db")
nodes = storage.list_nodes(project_id="my-project", status="active")

import json, pathlib
pathlib.Path("export/decisions.json").write_text(json.dumps(nodes, indent=2, default=str))
print(f"Exported {len(nodes)} nodes")
```

### Export the full event ledger

```python
from src.memory.ledger import EventLedger

ledger = EventLedger(db_path="data/contextforge.db")
events = ledger.list_events(last_n=10000)  # all events

import json, pathlib
pathlib.Path("export/ledger.json").write_text(json.dumps(events, indent=2, default=str))
```

### Push data somewhere else

**To a REST API:**

```python
import httpx, json
from src.core.storage import StorageAdapter

nodes = StorageAdapter("data/contextforge.db").list_nodes("my-project")
httpx.post("https://your-api.example.com/import", json={"nodes": nodes})
```

**To Postgres:**

```python
import psycopg2, json
from src.core.storage import StorageAdapter

conn = psycopg2.connect("postgresql://user:pass@host/db")
cur = conn.cursor()
for node in StorageAdapter("data/contextforge.db").list_nodes("my-project"):
    cur.execute(
        "INSERT INTO knowledge_nodes (id, project_id, area, summary, rationale, confidence) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
        (node["id"], node["project_id"], node["area"], node["summary"], node.get("rationale",""), node.get("confidence",0.5))
    )
conn.commit()
```

**Copy the database file directly:**

The `data/contextforge.db` is a standard SQLite file. Copy it anywhere — it's self-contained.

```bash
cp data/contextforge.db ~/Backups/contextforge-$(date +%Y%m%d).db
```

---

## 4. Run the Benchmarks

### Full validation (375 tests, ~2 min)

```bash
python -X utf8 benchmark/test_v5/run_all.py
```

All five suites run sequentially. Expected: `375/375 passed`.

### Individual suites

```bash
python -X utf8 benchmark/test_v5/iter_01_core.py    # Circuit breaker (~5 s)
python -X utf8 benchmark/test_v5/iter_02_ledger.py  # Hash chain (~37 s)
python -X utf8 benchmark/test_v5/iter_03_poison.py  # Adversarial guard (~6 s)
python -X utf8 benchmark/test_v5/iter_04_scale.py   # DCI token budget (~7 s)
python -X utf8 benchmark/test_v5/iter_05_chaos.py   # Chaos / 500 writers (~45 s)
python -X utf8 benchmark/test_v5/iter_06_adversarial_boundary.py  # Entropy boundary (75 tests)
```

### Scientific dual-pass benchmark

```bash
python -X utf8 benchmark/engine.py
```

Runs 100 probes in two modes (Stateless RAG vs ContextForge Nexus). Writes results to `data/academic_metrics.json`. Takes 2–3 minutes.

### Regenerate charts

```bash
python -X utf8 benchmark/generate_viz.py
```

Outputs six publication-quality PNGs to `docs/assets/` at 300 DPI. Requires `matplotlib`.

---

## 5. What Each Check Tells You

| Check | What to look for | Meaning |
|-------|-----------------|---------|
| `iter_01_core.py` passes | Circuit breaker state machine works | Router failover is safe |
| `iter_02_ledger.py` passes | Hash chain integrity holds | Ledger is tamper-evident |
| `iter_03_poison.py` passes | ReviewerGuard blocks injection | Security gate is active |
| `iter_04_scale.py` passes | DCI token budget enforced | No token overflow |
| `iter_05_chaos.py` passes | 500 concurrent writers survived | Production-grade under load |
| `data/contextforge.db` exists | Database is initialized | Storage ready |
| `list_events` returns events | MCP writes are hitting DB | End-to-end connected |
| `search_context` returns chunks | Local indexer is working | RAG pipeline active |

---

## 6. Common Checks After Setup

**Check how many nodes are in the graph:**

```bash
sqlite3 data/contextforge.db "SELECT project_id, COUNT(*) FROM decision_nodes GROUP BY project_id;"
```

**Check the ledger size:**

```bash
sqlite3 data/contextforge.db "SELECT COUNT(*) as total_events, MAX(timestamp) as latest FROM events;"
```

**Check if entropy gate is blocking anything:**

```bash
sqlite3 data/contextforge.db "SELECT COUNT(*) FROM events WHERE event_type='CONFLICT';"
```

A non-zero count means the ReviewerGuard has blocked or quarantined writes — this is the security gate working correctly.

**Check snapshot files:**

```bash
ls -lh .forge/
```

Each `.forge` file is an AES-256-GCM encrypted snapshot. File sizes should be small (a few KB per snapshot).

---

*ContextForge Nexus Architecture — reproducible, information-theoretically grounded agentic memory.*
