/**
 * ContextForge Nexus — TypeScript MCP Server v5.1.0
 *
 * Tools:
 *   get_knowledge_node   Retrieve a decision node by ID (L0/L1/L2)
 *   init_project         Register a new project
 *   capture_decision     Append a decision node to the graph
 *   load_context         Hierarchical context assembly (L0/L1/L2)
 *   list_events          Inspect the event ledger
 *   list_projects        List all registered projects
 *   rename_project       Rename a project display name
 *   list_decisions       List decision nodes with optional filters
 *   project_stats        Return statistics for a project
 *   list_tasks           List tasks for a project
 *   create_task          Create a new task in a project
 *   update_task          Update the status of an existing task
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
const DB_PATH   = process.env.DB_PATH ?? path.resolve(__dirname, "../data/contextforge.db");

function openDb(readonly = true): Database.Database {
  const db = new Database(DB_PATH, { readonly });
  db.pragma("journal_mode = WAL");
  db.pragma("foreign_keys = ON");
  return db;
}

function parseJson<T>(val: unknown, fallback: T): T {
  if (typeof val !== "string") return fallback;
  try { return JSON.parse(val) as T; } catch { return fallback; }
}

function nowIso(): string {
  return new Date().toISOString().replace("T", " ").split(".")[0];
}

const server = new McpServer({ name: "contextforge-nexus", version: "5.1.0" });

// ── get_knowledge_node ──────────────────────────────────────────────────────

server.tool(
  "get_knowledge_node",
  "Retrieve a single decision node by UUID. Returns WHY the decision was made at the requested detail level.",
  {
    node_id:      z.string().uuid().describe("UUID of the decision node"),
    detail_level: z.enum(["L0","L1","L2"]).default("L1").describe("L0=one-line; L1=+rationale; L2=full provenance"),
  },
  async ({ node_id, detail_level }) => {
    const db = openDb(true);
    try {
      const row = db
        .prepare("SELECT * FROM decision_nodes WHERE id = ? AND tombstone = 0")
        .get(node_id) as Record<string, unknown> | undefined;

      if (!row) {
        return { content: [{ type: "text" as const, text: JSON.stringify({ error: "node_not_found", node_id }) }] };
      }

      const alts = parseJson<Array<{ option?: string }>>(row.alternatives as string, []);
      const deps  = parseJson<string[]>(row.dependencies as string, []);

      let text: string;
      if (detail_level === "L0") {
        text = `[${row.area}] ${row.summary}`;
      } else if (detail_level === "L1") {
        text = [
          `[${row.area}] ${row.summary}`,
          `Rationale: ${row.rationale ?? "—"}`,
          `Confidence: ${Number(row.confidence).toFixed(2)}`,
          `Status: ${row.status}`,
        ].join("\n");
      } else {
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
      return { content: [{ type: "text" as const, text }] };
    } finally {
      db.close();
    }
  }
);

// ── init_project ────────────────────────────────────────────────────────────

server.tool(
  "init_project",
  "Register a new project in the ContextForge knowledge graph.",
  {
    project_id:   z.string().describe("Unique project slug"),
    name:         z.string().describe("Human-readable name"),
    project_type: z.enum(["code","research","study","general","custom"]).default("code"),
    description:  z.string().optional(),
    goals:        z.array(z.string()).default([]),
    tech_stack:   z.record(z.string()).default({}),
  },
  async ({ project_id, name, project_type, description, goals, tech_stack }) => {
    const db = openDb(false);
    try {
      db.prepare(`
        INSERT INTO projects (id, name, project_type, description, goals, tech_stack, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          name=excluded.name, project_type=excluded.project_type,
          description=excluded.description, goals=excluded.goals,
          tech_stack=excluded.tech_stack, updated_at=excluded.updated_at
      `).run(
        project_id, name, project_type,
        description ?? "",
        JSON.stringify(goals),
        JSON.stringify(tech_stack),
        nowIso(), nowIso(),
      );
      return { content: [{ type: "text" as const, text: JSON.stringify({ status: "created", project_id, name }) }] };
    } finally {
      db.close();
    }
  }
);

// ── capture_decision ────────────────────────────────────────────────────────

server.tool(
  "capture_decision",
  "Append a decision node to the knowledge graph. Records WHY a decision was made, alternatives, and causal dependencies.",
  {
    project_id:   z.string(),
    summary:      z.string().describe("One-line decision summary"),
    rationale:    z.string().default("Rationale not explicitly stated."),
    area:         z.string().describe("e.g. auth, database, api-design"),
    alternatives: z.array(z.object({
      option:           z.string(),
      rejected_because: z.string().optional(),
    })).default([]),
    confidence:   z.number().min(0).max(1).default(0.8),
    file_refs:    z.array(z.string()).default([]),
  },
  async ({ project_id, summary, rationale, area, alternatives, confidence, file_refs }) => {
    const db = openDb(false);
    try {
      const node_id = crypto.randomUUID();
      const now     = nowIso();
      db.prepare(`
        INSERT INTO decision_nodes
          (id, project_id, summary, rationale, area, alternatives, dependencies,
           confidence, importance, vclock, origin_client, tombstone,
           created_by_agent, status, type_metadata, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      `).run(
        node_id, project_id, summary, rationale, area,
        JSON.stringify(alternatives), JSON.stringify([]),
        confidence, 0.5,
        JSON.stringify({}), "mcp-client", 0,
        "mcp-client", "active",
        JSON.stringify({ file_refs, packages: [] }),
        now, now,
      );
      return { content: [{ type: "text" as const, text: JSON.stringify({ status: "captured", node_id, project_id }) }] };
    } finally {
      db.close();
    }
  }
);

// ── load_context ────────────────────────────────────────────────────────────

server.tool(
  "load_context",
  "Load hierarchical context for a project. L0=abstract; L1=+decision summaries; L2=+full rationale+alternatives.",
  {
    project_id:   z.string(),
    query:        z.string().optional().describe("Topic filter (keyword search on summary/area)"),
    detail_level: z.enum(["L0","L1","L2"]).default("L1"),
    top_k:        z.number().int().default(10),
  },
  async ({ project_id, query, detail_level, top_k }) => {
    const db = openDb(true);
    try {
      const proj = db
        .prepare("SELECT * FROM projects WHERE id = ?")
        .get(project_id) as Record<string, unknown> | undefined;

      if (!proj) {
        return { content: [{ type: "text" as const, text: JSON.stringify({ error: `Project '${project_id}' not found. Call init_project first.` }) }] };
      }

      const l0 = {
        level: "L0", project_id,
        name:        proj.name,
        type:        proj.project_type,
        description: proj.description,
        tech_stack:  parseJson(proj.tech_stack, {}),
        goals:       parseJson(proj.goals, []),
      };

      if (detail_level === "L0") {
        return { content: [{ type: "text" as const, text: JSON.stringify(l0, null, 2) }] };
      }

      let rows: Record<string, unknown>[];
      if (query) {
        const like = `%${query}%`;
        rows = db.prepare(`
          SELECT * FROM decision_nodes
          WHERE project_id = ? AND tombstone = 0 AND status = 'active'
            AND (summary LIKE ? OR area LIKE ? OR rationale LIKE ?)
          ORDER BY importance DESC, created_at DESC LIMIT ?
        `).all(project_id, like, like, like, top_k) as Record<string, unknown>[];
      } else {
        rows = db.prepare(`
          SELECT * FROM decision_nodes
          WHERE project_id = ? AND tombstone = 0 AND status = 'active'
          ORDER BY importance DESC, created_at DESC LIMIT ?
        `).all(project_id, top_k) as Record<string, unknown>[];
      }

      if (detail_level === "L1") {
        const decisions = rows.map(r => ({ id: r.id, area: r.area, summary: r.summary, confidence: r.confidence }));
        return { content: [{ type: "text" as const, text: JSON.stringify({ ...l0, level: "L1", decisions }, null, 2) }] };
      }

      const decisions = rows.map(r => ({
        id: r.id, area: r.area, summary: r.summary, rationale: r.rationale,
        alternatives: parseJson(r.alternatives, []),
        dependencies: parseJson(r.dependencies, []),
        confidence: r.confidence, status: r.status,
        created_by_agent: r.created_by_agent,
        type_metadata: parseJson(r.type_metadata, {}),
      }));
      return { content: [{ type: "text" as const, text: JSON.stringify({ ...l0, level: "L2", decisions }, null, 2) }] };
    } finally {
      db.close();
    }
  }
);

// ── list_events ─────────────────────────────────────────────────────────────

server.tool(
  "list_events",
  "Inspect the event ledger — append-only record of all agent activity.",
  {
    last_n:     z.number().int().default(20).describe("Number of recent events"),
    event_type: z.string().optional().describe("Filter by event type e.g. AGENT_THOUGHT"),
  },
  async ({ last_n, event_type }) => {
    const db = openDb(true);
    try {
      let rows: Record<string, unknown>[];
      if (event_type) {
        rows = db.prepare(
          "SELECT * FROM events WHERE event_type = ? ORDER BY created_at DESC LIMIT ?"
        ).all(event_type, last_n) as Record<string, unknown>[];
      } else {
        rows = db.prepare(
          "SELECT * FROM events ORDER BY created_at DESC LIMIT ?"
        ).all(last_n) as Record<string, unknown>[];
      }
      const events = rows.map(r => ({
        event_id: r.event_id, event_type: r.event_type,
        status: r.status, created_at: r.created_at,
        content: parseJson(r.content, r.content),
      }));
      return { content: [{ type: "text" as const, text: JSON.stringify(events, null, 2) }] };
    } finally {
      db.close();
    }
  }
);

// ── list_projects ───────────────────────────────────────────────────────────

server.tool(
  "list_projects",
  "List all registered projects in this ContextForge instance.",
  {},
  async () => {
    const db = openDb(true);
    try {
      const rows = db.prepare(
        "SELECT id, name, project_type, description, created_at FROM projects ORDER BY created_at DESC"
      ).all() as Record<string, unknown>[];
      return { content: [{ type: "text" as const, text: JSON.stringify({ projects: rows, count: rows.length }, null, 2) }] };
    } finally {
      db.close();
    }
  }
);

// ── rename_project ──────────────────────────────────────────────────────────

server.tool(
  "rename_project",
  "Rename a project display name. The project_id slug does not change.",
  {
    project_id:      z.string().min(2).describe("Project ID to rename"),
    new_name:        z.string().min(1).describe("New display name"),
    new_description: z.string().optional().describe("New description (omit to keep existing)"),
  },
  async ({ project_id, new_name, new_description }) => {
    const db = openDb(false);
    try {
      const exists = db.prepare("SELECT 1 FROM projects WHERE id = ?").get(project_id);
      if (!exists) {
        return { content: [{ type: "text" as const, text: JSON.stringify({ error: `Project '${project_id}' not found` }) }] };
      }
      if (new_description !== undefined) {
        db.prepare("UPDATE projects SET name = ?, description = ?, updated_at = ? WHERE id = ?")
          .run(new_name, new_description, nowIso(), project_id);
      } else {
        db.prepare("UPDATE projects SET name = ?, updated_at = ? WHERE id = ?")
          .run(new_name, nowIso(), project_id);
      }
      return { content: [{ type: "text" as const, text: JSON.stringify({ status: "renamed", project_id, new_name }) }] };
    } finally {
      db.close();
    }
  }
);

// ── list_decisions ──────────────────────────────────────────────────────────

server.tool(
  "list_decisions",
  "List decision nodes for a project with optional area and status filters.",
  {
    project_id: z.string().min(2).describe("Project ID"),
    area:       z.string().optional().describe("Filter by area (e.g. 'auth', 'database')"),
    status:     z.enum(["active","deprecated","quarantined","pending"]).default("active"),
    limit:      z.number().int().default(20),
  },
  async ({ project_id, area, status, limit }) => {
    const db = openDb(true);
    try {
      let rows: Record<string, unknown>[];
      if (area) {
        rows = db.prepare(
          "SELECT id, area, summary, rationale, confidence, status, created_at FROM decision_nodes WHERE project_id = ? AND area = ? AND tombstone = 0 AND status = ? ORDER BY importance DESC LIMIT ?"
        ).all(project_id, area, status, limit) as Record<string, unknown>[];
      } else {
        rows = db.prepare(
          "SELECT id, area, summary, rationale, confidence, status, created_at FROM decision_nodes WHERE project_id = ? AND tombstone = 0 AND status = ? ORDER BY importance DESC LIMIT ?"
        ).all(project_id, status, limit) as Record<string, unknown>[];
      }
      return { content: [{ type: "text" as const, text: JSON.stringify({ project_id, count: rows.length, decisions: rows }, null, 2) }] };
    } finally {
      db.close();
    }
  }
);

// ── project_stats ───────────────────────────────────────────────────────────

server.tool(
  "project_stats",
  "Return statistics for a project: node counts by area and status, task completion.",
  {
    project_id: z.string().min(2).describe("Project ID"),
  },
  async ({ project_id }) => {
    const db = openDb(true);
    try {
      const proj = db.prepare("SELECT * FROM projects WHERE id = ?").get(project_id) as Record<string, unknown> | undefined;
      if (!proj) {
        return { content: [{ type: "text" as const, text: JSON.stringify({ error: `Project '${project_id}' not found` }) }] };
      }
      const activeNodes = (db.prepare("SELECT COUNT(*) as cnt FROM decision_nodes WHERE project_id = ? AND tombstone = 0 AND status = 'active'").get(project_id) as Record<string,number>).cnt;
      const totalNodes  = (db.prepare("SELECT COUNT(*) as cnt FROM decision_nodes WHERE project_id = ? AND tombstone = 0").get(project_id) as Record<string,number>).cnt;
      const areaRows    = db.prepare("SELECT area, COUNT(*) as cnt FROM decision_nodes WHERE project_id = ? AND tombstone = 0 AND status = 'active' GROUP BY area ORDER BY cnt DESC").all(project_id) as Record<string, unknown>[];
      const taskRows    = db.prepare("SELECT status, COUNT(*) as cnt FROM tasks WHERE project_id = ? GROUP BY status").all(project_id) as Record<string, unknown>[];
      const taskCounts  = Object.fromEntries(taskRows.map(r => [r.status as string, r.cnt as number]));
      const totalTasks  = taskRows.reduce((s, r) => s + (r.cnt as number), 0);
      return { content: [{ type: "text" as const, text: JSON.stringify({
        project_id, name: proj.name, project_type: proj.project_type,
        nodes: { total: totalNodes, active: activeNodes },
        areas: areaRows,
        tasks: { ...taskCounts, total: totalTasks, pct_complete: totalTasks ? Math.round(((taskCounts.done ?? 0) / totalTasks) * 100) : 0 },
      }, null, 2) }] };
    } finally {
      db.close();
    }
  }
);

// ── list_tasks ──────────────────────────────────────────────────────────────

server.tool(
  "list_tasks",
  "List tasks for a project with optional status filter.",
  {
    project_id: z.string().min(2).describe("Project ID"),
    status:     z.enum(["pending","in_progress","done","blocked"]).optional(),
    limit:      z.number().int().default(20),
  },
  async ({ project_id, status, limit }) => {
    const db = openDb(true);
    try {
      let rows: Record<string, unknown>[];
      if (status) {
        rows = db.prepare("SELECT * FROM tasks WHERE project_id = ? AND status = ? ORDER BY priority ASC, created_at ASC LIMIT ?").all(project_id, status, limit) as Record<string, unknown>[];
      } else {
        rows = db.prepare("SELECT * FROM tasks WHERE project_id = ? ORDER BY priority ASC, created_at ASC LIMIT ?").all(project_id, limit) as Record<string, unknown>[];
      }
      return { content: [{ type: "text" as const, text: JSON.stringify({ project_id, count: rows.length, tasks: rows }, null, 2) }] };
    } finally {
      db.close();
    }
  }
);

// ── create_task ─────────────────────────────────────────────────────────────

server.tool(
  "create_task",
  "Create a new task in a project.",
  {
    project_id:  z.string().min(2).describe("Project ID"),
    title:       z.string().min(1).describe("Task title"),
    description: z.string().default(""),
    priority:    z.number().int().min(1).max(5).default(3).describe("1=highest, 5=lowest"),
    sprint:      z.string().default(""),
    assigned_to: z.string().default(""),
  },
  async ({ project_id, title, description, priority, sprint, assigned_to }) => {
    const db = openDb(false);
    try {
      const proj = db.prepare("SELECT 1 FROM projects WHERE id = ?").get(project_id);
      if (!proj) {
        // Auto-create stub project
        db.prepare("INSERT OR IGNORE INTO projects (id, name, project_type) VALUES (?, ?, 'general')").run(project_id, project_id);
      }
      const taskId = crypto.randomUUID();
      const now = nowIso();
      db.prepare(
        "INSERT INTO tasks (id, project_id, title, description, status, priority, sprint, assigned_to, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
      ).run(taskId, project_id, title, description, "pending", priority, sprint, assigned_to, now, now);
      return { content: [{ type: "text" as const, text: JSON.stringify({ status: "created", task_id: taskId, title }) }] };
    } finally {
      db.close();
    }
  }
);

// ── update_task ─────────────────────────────────────────────────────────────

server.tool(
  "update_task",
  "Update the status of an existing task.",
  {
    task_id: z.string().describe("Task ID"),
    status:  z.enum(["pending","in_progress","done","blocked"]),
  },
  async ({ task_id, status }) => {
    const db = openDb(false);
    try {
      const result = db.prepare("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?").run(status, nowIso(), task_id);
      if (result.changes === 0) {
        return { content: [{ type: "text" as const, text: JSON.stringify({ error: `Task '${task_id}' not found` }) }] };
      }
      return { content: [{ type: "text" as const, text: JSON.stringify({ status: "updated", task_id, new_status: status }) }] };
    } finally {
      db.close();
    }
  }
);

// ── Start ───────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  process.stderr.write(`ContextForge Nexus MCP v5.1.0 (TypeScript) — db=${DB_PATH}\n`);
}

main().catch(err => { process.stderr.write(`Fatal: ${err}\n`); process.exit(1); });
