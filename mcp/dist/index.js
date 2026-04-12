/**
 * ContextForge Nexus — TypeScript MCP Server v5.0.0
 *
 * Tools:
 *   get_knowledge_node   Retrieve a decision node by ID (L0/L1/L2)
 *   init_project         Register a new project
 *   capture_decision     Append a decision node to the graph
 *   load_context         Hierarchical context assembly (L0/L1/L2)
 *   list_events          Inspect the event ledger
 *
 * Usage:
 *   npm run dev    # tsx watch (no compile)
 *   npm run build && npm start
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import Database from "better-sqlite3";
import crypto from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { z } from "zod";
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DB_PATH = process.env.DB_PATH ?? path.resolve(__dirname, "../data/contextforge.db");
function openDb(readonly = true) {
    const db = new Database(DB_PATH, { readonly });
    db.pragma("journal_mode = WAL");
    db.pragma("foreign_keys = ON");
    return db;
}
function parseJson(val, fallback) {
    if (typeof val !== "string")
        return fallback;
    try {
        return JSON.parse(val);
    }
    catch {
        return fallback;
    }
}
function nowIso() {
    return new Date().toISOString().replace("T", " ").split(".")[0];
}
const server = new McpServer({ name: "contextforge-nexus", version: "5.0.0" });
// ── get_knowledge_node ──────────────────────────────────────────────────────
server.tool("get_knowledge_node", "Retrieve a single decision node by UUID. Returns WHY the decision was made at the requested detail level.", {
    node_id: z.string().uuid().describe("UUID of the decision node"),
    detail_level: z.enum(["L0", "L1", "L2"]).default("L1").describe("L0=one-line; L1=+rationale; L2=full provenance"),
}, async ({ node_id, detail_level }) => {
    const db = openDb(true);
    try {
        const row = db
            .prepare("SELECT * FROM decision_nodes WHERE id = ? AND tombstone = 0")
            .get(node_id);
        if (!row) {
            return { content: [{ type: "text", text: JSON.stringify({ error: "node_not_found", node_id }) }] };
        }
        const alts = parseJson(row.alternatives, []);
        const deps = parseJson(row.dependencies, []);
        let text;
        if (detail_level === "L0") {
            text = `[${row.area}] ${row.summary}`;
        }
        else if (detail_level === "L1") {
            text = [
                `[${row.area}] ${row.summary}`,
                `Rationale: ${row.rationale ?? "—"}`,
                `Confidence: ${Number(row.confidence).toFixed(2)}`,
                `Status: ${row.status}`,
            ].join("\n");
        }
        else {
            text = [
                `[${row.area}] ${row.summary}`,
                `Rationale: ${row.rationale ?? "—"}`,
                `Alternatives: ${alts.map(a => a.option ?? "?").join(", ") || "—"}`,
                `Dependencies: ${deps.slice(0, 5).join(", ") || "—"}`,
                `Created by: ${row.created_by_agent} | Validated: ${row.validated_by ?? "—"}`,
                `Confidence: ${Number(row.confidence).toFixed(2)} | Status: ${row.status}`,
                `Origin: ${row.origin_client ?? "—"}`,
            ].join("\n");
        }
        return { content: [{ type: "text", text }] };
    }
    finally {
        db.close();
    }
});
// ── init_project ────────────────────────────────────────────────────────────
server.tool("init_project", "Register a new project in the ContextForge knowledge graph.", {
    project_id: z.string().describe("Unique project slug"),
    name: z.string().describe("Human-readable name"),
    project_type: z.enum(["code", "research", "study", "general", "custom"]).default("code"),
    description: z.string().optional(),
    goals: z.array(z.string()).default([]),
    tech_stack: z.record(z.string()).default({}),
}, async ({ project_id, name, project_type, description, goals, tech_stack }) => {
    const db = openDb(false);
    try {
        db.prepare(`
        INSERT INTO projects (id, name, project_type, description, goals, tech_stack, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          name=excluded.name, project_type=excluded.project_type,
          description=excluded.description, goals=excluded.goals,
          tech_stack=excluded.tech_stack, updated_at=excluded.updated_at
      `).run(project_id, name, project_type, description ?? "", JSON.stringify(goals), JSON.stringify(tech_stack), nowIso(), nowIso());
        return { content: [{ type: "text", text: JSON.stringify({ status: "created", project_id, name }) }] };
    }
    finally {
        db.close();
    }
});
// ── capture_decision ────────────────────────────────────────────────────────
server.tool("capture_decision", "Append a decision node to the knowledge graph. Records WHY a decision was made, alternatives, and causal dependencies.", {
    project_id: z.string(),
    summary: z.string().describe("One-line decision summary"),
    rationale: z.string().default("Rationale not explicitly stated."),
    area: z.string().describe("e.g. auth, database, api-design"),
    alternatives: z.array(z.object({
        option: z.string(),
        rejected_because: z.string().optional(),
    })).default([]),
    confidence: z.number().min(0).max(1).default(0.8),
    file_refs: z.array(z.string()).default([]),
}, async ({ project_id, summary, rationale, area, alternatives, confidence, file_refs }) => {
    const db = openDb(false);
    try {
        const node_id = crypto.randomUUID();
        const now = nowIso();
        db.prepare(`
        INSERT INTO decision_nodes
          (id, project_id, summary, rationale, area, alternatives, dependencies,
           confidence, importance, vclock, origin_client, tombstone,
           created_by_agent, status, type_metadata, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      `).run(node_id, project_id, summary, rationale, area, JSON.stringify(alternatives), JSON.stringify([]), confidence, 0.5, JSON.stringify({}), "mcp-client", 0, "mcp-client", "active", JSON.stringify({ file_refs, packages: [] }), now, now);
        return { content: [{ type: "text", text: JSON.stringify({ status: "captured", node_id, project_id }) }] };
    }
    finally {
        db.close();
    }
});
// ── load_context ────────────────────────────────────────────────────────────
server.tool("load_context", "Load hierarchical context for a project. L0=abstract; L1=+decision summaries; L2=+full rationale+alternatives.", {
    project_id: z.string(),
    query: z.string().optional().describe("Topic filter (keyword search on summary/area)"),
    detail_level: z.enum(["L0", "L1", "L2"]).default("L1"),
    top_k: z.number().int().default(10),
}, async ({ project_id, query, detail_level, top_k }) => {
    const db = openDb(true);
    try {
        const proj = db
            .prepare("SELECT * FROM projects WHERE id = ?")
            .get(project_id);
        if (!proj) {
            return { content: [{ type: "text", text: JSON.stringify({ error: `Project '${project_id}' not found. Call init_project first.` }) }] };
        }
        const l0 = {
            level: "L0", project_id,
            name: proj.name,
            type: proj.project_type,
            description: proj.description,
            tech_stack: parseJson(proj.tech_stack, {}),
            goals: parseJson(proj.goals, []),
        };
        if (detail_level === "L0") {
            return { content: [{ type: "text", text: JSON.stringify(l0, null, 2) }] };
        }
        let rows;
        if (query) {
            const like = `%${query}%`;
            rows = db.prepare(`
          SELECT * FROM decision_nodes
          WHERE project_id = ? AND tombstone = 0 AND status = 'active'
            AND (summary LIKE ? OR area LIKE ? OR rationale LIKE ?)
          ORDER BY importance DESC, created_at DESC LIMIT ?
        `).all(project_id, like, like, like, top_k);
        }
        else {
            rows = db.prepare(`
          SELECT * FROM decision_nodes
          WHERE project_id = ? AND tombstone = 0 AND status = 'active'
          ORDER BY importance DESC, created_at DESC LIMIT ?
        `).all(project_id, top_k);
        }
        if (detail_level === "L1") {
            const decisions = rows.map(r => ({ id: r.id, area: r.area, summary: r.summary, confidence: r.confidence }));
            return { content: [{ type: "text", text: JSON.stringify({ ...l0, level: "L1", decisions }, null, 2) }] };
        }
        const decisions = rows.map(r => ({
            id: r.id, area: r.area, summary: r.summary, rationale: r.rationale,
            alternatives: parseJson(r.alternatives, []),
            dependencies: parseJson(r.dependencies, []),
            confidence: r.confidence, status: r.status,
            created_by_agent: r.created_by_agent,
            type_metadata: parseJson(r.type_metadata, {}),
        }));
        return { content: [{ type: "text", text: JSON.stringify({ ...l0, level: "L2", decisions }, null, 2) }] };
    }
    finally {
        db.close();
    }
});
// ── list_events ─────────────────────────────────────────────────────────────
server.tool("list_events", "Inspect the event ledger — append-only record of all agent activity.", {
    last_n: z.number().int().default(20).describe("Number of recent events"),
    event_type: z.string().optional().describe("Filter by event type e.g. AGENT_THOUGHT"),
}, async ({ last_n, event_type }) => {
    const db = openDb(true);
    try {
        let rows;
        if (event_type) {
            rows = db.prepare("SELECT * FROM events WHERE event_type = ? ORDER BY created_at DESC LIMIT ?").all(event_type, last_n);
        }
        else {
            rows = db.prepare("SELECT * FROM events ORDER BY created_at DESC LIMIT ?").all(last_n);
        }
        const events = rows.map(r => ({
            event_id: r.event_id, event_type: r.event_type,
            status: r.status, created_at: r.created_at,
            content: parseJson(r.content, r.content),
        }));
        return { content: [{ type: "text", text: JSON.stringify(events, null, 2) }] };
    }
    finally {
        db.close();
    }
});
// ── Start ───────────────────────────────────────────────────────────────────
async function main() {
    const transport = new StdioServerTransport();
    await server.connect(transport);
    process.stderr.write(`ContextForge Nexus MCP v5.0.0 (TypeScript) — db=${DB_PATH}\n`);
}
main().catch(err => { process.stderr.write(`Fatal: ${err}\n`); process.exit(1); });
