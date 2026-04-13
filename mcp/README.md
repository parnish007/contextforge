# ContextForge MCP Server

Standalone MCP server that wraps the ContextForge five-pillar agentic memory
architecture. Connect to any MCP-compatible IDE — Claude Desktop, Cursor, VS Code,
Windsurf — and get persistent memory, rollback, and semantic search as native tools.

Both servers expose all **22 tools**. Choose based on your runtime preference:

| | Python server (`mcp/server.py`) | TypeScript server (`mcp/index.ts`) |
|---|---|---|
| **Runtime** | Python 3.10+ | Node.js 18+ |
| **Tools** | 22 | 22 |
| **Charter guard** | ✅ ReviewerGuard active | ❌ Writes directly to SQLite |
| **Semantic search** | ✅ sentence-transformers / TF-IDF | Keyword (word-match) fallback |
| **Snapshots** | AES-256-GCM (cryptography lib) | AES-256-GCM (Node crypto built-in) |
| **Best for** | Production, safety enforcement | Quick start, Node-only environments |

---

## Quick start — Python server (recommended)

```bash
# From project root
pip install -r requirements.txt
cp .env.example .env
# Edit .env — set DB_PATH and optionally API keys

# Stdio mode (Claude Desktop / Cursor / VS Code)
python mcp/server.py --stdio

# SSE/HTTP mode (remote / cloud)
python mcp/server.py --sse --host 0.0.0.0 --port 8765
```

## Quick start — TypeScript server

```bash
cd mcp
npm install
npm run build
node dist/index.js    # Stdio mode (from mcp/ directory)
# or from project root:
node mcp/dist/index.js
```

---

## All 22 Tools

| Category | Tool | Description |
|----------|------|-------------|
| **Project** | `list_projects` | List all registered projects |
| | `init_project` | Create / update a project |
| | `rename_project` | Rename display name (slug unchanged) |
| | `merge_projects` | Merge source project into target (irreversible) |
| | `delete_project` | Delete project, archive nodes first |
| | `project_stats` | Node/task/area statistics |
| **Decision** | `capture_decision` | Store a decision with WHY + alternatives |
| | `load_context` | L0/L1/L2 hierarchical context assembly |
| | `get_knowledge_node` | Retrieve decision node by UUID or keyword |
| | `list_decisions` | List with area/status filters |
| | `update_decision` | Edit summary, rationale, area, confidence |
| | `deprecate_decision` | Mark deprecated with reason + replacement |
| | `link_decisions` | Create typed edge between two decisions |
| **Task** | `list_tasks` | List tasks (filter by status) |
| | `create_task` | Create a new task |
| | `update_task` | Update task status |
| **Ledger** | `rollback` | Time-travel undo by event_id or timestamp |
| | `snapshot` | AES-256-GCM encrypted `.forge` checkpoint |
| | `list_snapshots` | List all `.forge` snapshot files |
| | `replay_sync` | Restore events from a `.forge` snapshot |
| | `list_events` | Inspect the append-only event ledger |
| **Search** | `search_context` | Local file search (semantic in Python, keyword in TS) |

---

## IDE config snippets

Copy-paste configs are in `mcp/configs/`:

| File | IDE | Where to place it |
|------|-----|-------------------|
| `claude_desktop.json` | Claude Desktop | `~/.config/Claude/claude_desktop_config.json` (macOS/Linux) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows) |
| `cursor.json` | Cursor | `.cursor/mcp.json` in project root |
| `vscode.json` | VS Code | `.vscode/mcp.json` in project root |
| `windsurf.json` | Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| `sse_remote.json` | Any IDE | Remote server via SSE |
| `typescript.json` | Any IDE | TypeScript server (Node.js) |

---

## Fully local — no API keys needed

```bash
# Install Ollama: https://ollama.com/download
ollama pull llama3.3    # or llama3.2 for smaller machines
# In .env:
FALLBACK_CHAIN=ollama
OLLAMA_URL=http://localhost:11434
# Launch:
python mcp/server.py --stdio
```

---

## Full documentation

→ [`docs/WHAT_IS_THIS.md`](../docs/WHAT_IS_THIS.md) — what ContextForge is, data flow, worked example  
→ [`docs/SETUP.md`](../docs/SETUP.md) — complete guide: all IDEs, API keys, Ollama, SSE remote, troubleshooting  
→ [`research/RESEARCH.md`](../research/RESEARCH.md) — v2 paper, figures, benchmark archives
