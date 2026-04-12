# ContextForge MCP Server

Standalone MCP server that wraps the ContextForge five-pillar agentic memory
architecture. Connect to any MCP-compatible IDE — Claude Desktop, Cursor, VS Code,
Windsurf — and get persistent memory, rollback, and semantic search as native tools.

## Quick start — Python server (9 tools)

```bash
# From project root
pip install -r requirements.txt
cp .env.example .env
# Edit .env — set DB_PATH and optionally an API key

# Stdio mode (Claude Desktop / Cursor / VS Code)
python mcp/server.py --stdio

# SSE/HTTP mode (remote / cloud)
python mcp/server.py --sse --host 0.0.0.0 --port 8765
```

## Quick start — TypeScript server (5 tools)

```bash
cd mcp
npm install --ignore-scripts   # skips native gyp compile (works on all platforms)
npm run build
node dist/index.js    # Stdio mode (from mcp/ directory)
# or from project root:
node mcp/dist/index.js
```

## Tools

| Tool | Server | Description |
|------|--------|-------------|
| `get_knowledge_node` | Python + TS | Query decision graph by topic |
| `init_project` | Python + TS | Register a new project |
| `capture_decision` | Python + TS | Store decision with WHY + alternatives |
| `load_context` | Python + TS | L0/L1/L2 hierarchical context |
| `search_context` | Python | Local-edge semantic file search (zero cloud tokens) |
| `rollback` | Python | Time-travel undo via append-only ledger |
| `snapshot` | Python | AES-256-GCM encrypted checkpoint |
| `replay_sync` | Python | Cross-device context restore from `.forge` file |
| `list_events` | Python + TS | Inspect agent activity ledger |

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

## Full documentation

→ [`docs/MCP_SETUP.md`](../docs/MCP_SETUP.md) — complete guide with all IDEs,
API key setup, Ollama, SSE remote, troubleshooting, and security notes.
