# ContextForge — Setup Guide

> **Author:** Trilochan Sharma — Independent Researcher · [parnish007](https://github.com/parnish007)

← [README](../README.md) · [What is this?](WHAT_IS_THIS.md) · [How to Use](HOW_TO_USE.md) · [Architecture](ARCHITECTURE.md)

ContextForge exposes its five-pillar agentic memory architecture as a native MCP
(Model Context Protocol) server. Any MCP-compatible IDE connects to it and uses
persistent memory, rollback, semantic search, and encrypted snapshots as built-in
tools — solving **context amnesia** across sessions and IDE switches.

---

## What you get

| Tool | What it does |
|------|-------------|
| `get_knowledge_node` | Query the knowledge graph — WHY decisions were made, alternatives, causal chain |
| `init_project` | Register a project in the persistent graph |
| `capture_decision` | Store a decision with rationale + alternatives (passes entropy gate) |
| `load_context` | Load L0/L1/L2 hierarchical context for a project |
| `search_context` | Semantic search over local files — zero cloud tokens |
| `rollback` | Time-travel undo — prune ledger to a prior event |
| `snapshot` | AES-256-GCM encrypted `.forge` checkpoint |
| `replay_sync` | Restore from a `.forge` snapshot (cross-device handshake) |
| `list_events` | Inspect all agent activity in the append-only ledger |

---

## Prerequisites

- **Python 3.10+** — for the Python MCP server (`mcp/server.py`)
- **Node.js 18+** — for the TypeScript server (`mcp/index.ts`) — optional alternative
- Git

---

## Step 1: Clone and install

```bash
git clone https://github.com/parnish007/contextforge.git
cd contextforge
pip install -r requirements.txt
```

---

## Step 2: Configure environment

```bash
cp .env.example .env
```

Open `.env` and set your configuration. **Minimum to run with no API keys:**

```ini
DB_PATH=./data/contextforge.db
CHARTER_PATH=PROJECT_CHARTER.md
FORGE_SNAPSHOT_KEY=change-this-to-a-random-passphrase
```

The system runs fully offline with no API keys — using Ollama locally or rule-based fallback.

---

## Step 3: Choose your LLM setup

### Option A: Fully local — Ollama (no API keys, no internet)

Best for: privacy-first, air-gapped, or low-cost setups.

1. Install Ollama: https://ollama.com/download
2. Pull a model:
   ```bash
   ollama pull llama3.3        # 70B — best quality, needs ~48 GB RAM
   ollama pull llama3.2        # 3B — fast, low memory
   ollama pull qwen2.5-coder   # Code-focused alternative
   ```
3. Start Ollama (runs on port 11434):
   ```bash
   ollama serve
   ```
4. Set in `.env`:
   ```ini
   FALLBACK_CHAIN=ollama
   OLLAMA_URL=http://localhost:11434
   OLLAMA_MODEL=llama3.3
   ```

### Option B: Groq — recommended (fast, generous free tier)

1. Sign up and get a free key: https://console.groq.com/
2. Set in `.env`:
   ```ini
   GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxx
   FALLBACK_CHAIN=groq,ollama
   ```

### Option C: Google Gemini

1. Get a key from AI Studio: https://aistudio.google.com/app/apikey
2. Set in `.env`:
   ```ini
   GEMINI_API_KEY=AIzaxxxxxxxxxxxxxxxxxxxxxxxx
   FALLBACK_CHAIN=groq,gemini,ollama
   ```

### Option D: Maximum resilience (all providers)

```ini
GROQ_API_KEY=gsk_...
GEMINI_API_KEY=AIza_...
OPENROUTER_API_KEY=sk-or-...
FALLBACK_CHAIN=groq,gemini,openrouter,ollama
```

The circuit breaker automatically fails over to the next provider on outage.

---

## Step 4: Initialize the database

The SQLite database is created automatically on first run. You can also seed it manually:

```bash
python -c "from src.core.storage import StorageAdapter; StorageAdapter('data/contextforge.db'); print('DB ready')"
```

---

## Step 5: Connect your IDE

### Claude Desktop

**Config file location:**
- macOS / Linux: `~/.config/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

**Add this to the config (merge with existing content):**

```json
{
  "mcpServers": {
    "contextforge": {
      "command": "python",
      "args": ["mcp/server.py", "--stdio"],
      "cwd": "/absolute/path/to/contextforge",
      "env": {
        "DB_PATH": "data/contextforge.db",
        "CHARTER_PATH": "PROJECT_CHARTER.md",
        "GROQ_API_KEY": "your-key-here"
      }
    }
  }
}
```

**Windows example:**
```json
{
  "mcpServers": {
    "contextforge": {
      "command": "python",
      "args": ["mcp/server.py", "--stdio"],
      "cwd": "C:/Users/YourName/Projects/contextforge",
      "env": {
        "DB_PATH": "data/contextforge.db"
      }
    }
  }
}
```

Restart Claude Desktop after saving. You should see "contextforge" in the MCP server list.

---

### Cursor

Create `.cursor/mcp.json` in the project root:

```json
{
  "mcpServers": {
    "contextforge": {
      "command": "python",
      "args": ["mcp/server.py", "--stdio"],
      "cwd": "${workspaceRoot}",
      "env": {
        "DB_PATH": "data/contextforge.db"
      }
    }
  }
}
```

Reload Cursor (`Ctrl+Shift+P` → "Reload Window") after saving.

For a **global config** (all projects), place in `~/.cursor/mcp.json` with an absolute `cwd` path.

---

### VS Code

> **Requires:** GitHub Copilot Chat extension + VS Code 1.99+. MCP support is built into
> Copilot Chat — no separate MCP extension needed.

Create `.vscode/mcp.json` in the project root:

```json
{
  "servers": {
    "contextforge": {
      "type": "stdio",
      "command": "python",
      "args": ["mcp/server.py", "--stdio"],
      "cwd": "${workspaceFolder}",
      "env": {
        "DB_PATH": "data/contextforge.db",
        "CHARTER_PATH": "PROJECT_CHARTER.md"
      }
    }
  }
}
```

Reload the window (`Ctrl+Shift+P` → "Developer: Reload Window") after saving.
The `contextforge` server will appear in the Copilot Chat tool picker (@ menu).

---

### Windsurf

Open `~/.codeium/windsurf/mcp_config.json` (create if it doesn't exist) and add:

```json
{
  "mcpServers": {
    "contextforge": {
      "command": "python",
      "args": ["/absolute/path/to/contextforge/mcp/server.py", "--stdio"],
      "env": {
        "DB_PATH": "/absolute/path/to/contextforge/data/contextforge.db"
      }
    }
  }
}
```

> Windsurf requires **absolute paths** — `${workspaceRoot}` does not expand here.

---

### Remote / Cloud (SSE mode)

Run ContextForge on a server and connect multiple IDE clients to it:

**On the server:**
```bash
pip install "mcp[sse]" uvicorn starlette
python mcp/server.py --sse --host 0.0.0.0 --port 8765
```

**In your IDE config:**
```json
{
  "mcpServers": {
    "contextforge": {
      "type": "sse",
      "url": "http://your-server-ip:8765/sse"
    }
  }
}
```

> **Security note:** In production, put a reverse proxy (nginx/Caddy) with TLS and
> authentication in front of the SSE endpoint. Never expose port 8765 directly.

---

## Step 6: Verify the connection

In your IDE, ask the AI assistant:

> "List the ContextForge MCP tools available."

You should see all 22 tools. Then test end-to-end:

> "Use init_project to create a project with id='my-project' and name='My Test Project'."
> "Use load_context with project_id='my-project' and detail_level='L0'."

---

## TypeScript server (alternative to Python)

The TypeScript server exposes all 22 tools and connects directly to SQLite. It has full parity with the Python server except that `ReviewerGuard` (charter enforcement) does not run — use the Python server if charter compliance checking is required.

```bash
cd mcp
npm install
npm run build
```

Then in your IDE config:
```json
{
  "mcpServers": {
    "contextforge": {
      "command": "node",
      "args": ["mcp/dist/index.js"],
      "cwd": "/absolute/path/to/contextforge",
      "env": {
        "DB_PATH": "data/contextforge.db"
      }
    }
  }
}
```

See `mcp/configs/typescript.json` for a copy-paste snippet.

---

## Web search setup (Researcher agent)

The `@research` director command uses tiered web search. Set at least one key:

```ini
# Best quality (free tier 1000 req/month):
TAVILY_API_KEY=tvly-xxxxxxxxxxxxxxxx

# Google search fallback:
SERPER_API_KEY=xxxxxxxxxxxxxxxx
```

DuckDuckGo is used automatically if neither key is set (no key required, but rate-limited).

---

## Snapshot encryption

The `snapshot` tool creates AES-256-GCM encrypted `.forge` files. Set a strong key:

```ini
FORGE_SNAPSHOT_KEY=my-very-long-random-passphrase-at-least-32-chars
```

Keep this key safe — without it you cannot decrypt existing snapshots.
The `.forge/` directory is gitignored by default.

---

## Troubleshooting

### "mcp package not installed"
```bash
pip install mcp
```

### "SSE transport requires uvicorn starlette"
```bash
pip install "mcp[sse]" uvicorn starlette
```

### "Project 'X' not found. Call init_project first."
You must call `init_project` before `load_context` or `capture_decision`.

### Database locked
Only one writer at a time. Multiple readers are fine (SQLite WAL mode). Don't run
two MCP server instances pointing at the same DB file simultaneously.

### Ollama connection refused
```bash
ollama serve          # Start Ollama if not running
ollama list           # Check available models
```

### No LLM responses (all providers failing)
The system falls back to rule-based processing automatically. Set `GROQ_API_KEY`
or run Ollama locally for LLM-powered agents.

### Windows: path errors in config
Use forward slashes or escaped backslashes:
```json
"cwd": "C:/Users/Name/Projects/contextforge"
```

### TypeScript build fails (`better-sqlite3` gyp error)
```bash
cd mcp && npm install --ignore-scripts && npm run build
```

The TypeScript server uses SQLite in read/write mode — `--ignore-scripts` skips
the native binary compile while still allowing the JS fallback path.

---

## Security architecture

ContextForge protects the memory ledger with a dual-signal entropy gate:

| Mechanism | Threshold | Catches |
|-----------|-----------|---------|
| Shannon entropy gate | H* = 3.5 bits | Obfuscated high-vocabulary payloads |
| LZ compression density | ρ_min = 0.60 | Repetition attacks |
| Tiered Clearance Logic (VOH) | H*_VOH ≈ 4.38 bits | Internal system traffic (elevated threshold) |
| ReviewerGuard | PROJECT_CHARTER.md | Destructive operations |

Benchmark result: **+85.0 pp adversarial block rate** vs. Stateless RAG baseline (0% → 85%).

External writes (user content, retrieved chunks, tool outputs) always traverse the
standard H* = 3.5 threshold. Internal system writes use the elevated VOH threshold
to reduce false positives on legitimate technical content.

---

## Directory structure

```
contextforge/
├── mcp/
│   ├── server.py          # Python MCP server — 9 tools, Stdio + SSE
│   ├── index.ts           # TypeScript MCP server — 22 tools (full parity)
│   ├── package.json       # npm package (contextforge-mcp)
│   ├── tsconfig.json
│   ├── dist/              # Compiled TypeScript (after npm run build)
│   │   └── index.js
│   └── configs/           # Copy-paste IDE config snippets
│       ├── claude_desktop.json
│       ├── cursor.json
│       ├── vscode.json
│       ├── windsurf.json
│       ├── sse_remote.json
│       └── typescript.json
├── src/                   # Core architecture (unchanged)
│   ├── memory/ledger.py   # Append-only event ledger + ReviewerGuard
│   ├── router/nexus_router.py  # Tri-core LLM failover + circuit breaker
│   ├── retrieval/         # Local-edge RAG + DCI cosine gate
│   ├── sync/fluid_sync.py # AES-256-GCM snapshots + idle checkpoint
│   └── transport/         # Original MCP transport (also usable standalone)
├── prompts/skills/        # Agent skill prompts (versioned)
│   ├── code-architecture/system.v1.md
│   ├── general-capture/system.v1.md
│   ├── shadow-reviewer/system.v1.md
│   ├── historian/system.v1.md
│   ├── researcher/system.v1.md
│   └── pm/system.v1.md
├── data/
│   └── contextforge.db    # SQLite knowledge graph (created on first run)
├── .env.example           # All environment variables documented
└── docs/
    └── SETUP.md           # This guide
```

---

## Quick reference

```bash
# Python server — Stdio (Claude Desktop, Cursor, VS Code)
python mcp/server.py --stdio

# Python server — SSE/HTTP (remote, multi-client)
python mcp/server.py --sse --host 0.0.0.0 --port 8765

# TypeScript server — first-time setup
cd mcp && npm install && npm run build
node mcp/dist/index.js     # from project root

# Verify end-to-end (run from project root)
python -X utf8 benchmark/test_v5/run_all.py   # 375 tests, ~2 min

# Re-generate publication charts
python -X utf8 benchmark/generate_viz.py      # → docs/assets/
```

| Goal | Minimum .env |
|------|-------------|
| Fully local, no internet | `FALLBACK_CHAIN=ollama` + Ollama running |
| Fast cloud (free) | `GROQ_API_KEY=gsk_...` |
| Max resilience | `GROQ_API_KEY` + `GEMINI_API_KEY` + Ollama |
| Encrypted snapshots | `FORGE_SNAPSHOT_KEY=<32+ chars>` |

---

*ContextForge Nexus Architecture — reproducible, information-theoretically grounded agentic memory.*
