/**
 * ContextForge Nexus — TypeScript MCP Server v5.2.0
 *
 * All 22 tools — full parity with Python server (mcp/server.py).
 *
 * Tools (22):
 *
 *   Project management (6):
 *     list_projects        List all registered projects
 *     init_project         Create / update a project
 *     rename_project       Rename a project display name
 *     merge_projects       Merge one project into another (irreversible)
 *     delete_project       Permanently delete a project (archives nodes first)
 *     project_stats        Node/task/area statistics for a project
 *
 *   Decision graph (7):
 *     capture_decision     Append a decision node to the knowledge graph
 *     load_context         Hierarchical context assembly (L0/L1/L2)
 *     get_knowledge_node   Retrieve a decision node by UUID
 *     list_decisions       List decisions with area/status filters
 *     update_decision      Edit fields on an existing decision node
 *     deprecate_decision   Mark a decision deprecated with reason
 *     link_decisions       Create a typed edge between two decisions
 *
 *   Tasks (3):
 *     list_tasks           List tasks for a project
 *     create_task          Create a new task
 *     update_task          Update task status
 *
 *   Ledger / memory (5):
 *     rollback             Time-travel undo (by event_id or timestamp)
 *     snapshot             AES-256-GCM encrypted checkpoint (.forge)
 *     list_snapshots       List all .forge snapshot files
 *     replay_sync          Restore events from a .forge snapshot
 *     list_events          Inspect the append-only event ledger
 *
 *   Search (1):
 *     search_context       Keyword search over local project files
 *
 * Usage:
 *   npm run dev    # tsx watch (no compile)
 *   npm run build && npm start
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import Database from "better-sqlite3";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { z } from "zod";

const deflate  = promisify(zlib.deflate);
const inflate  = promisify(zlib.inflate);

const __dirname  = path.dirname(fileURLToPath(import.meta.url));
const ROOT       = path.resolve(__dirname, "..");
const DB_PATH    = process.env.DB_PATH    ?? path.resolve(ROOT, "data/contextforge.db");
const FORGE_DIR  = process.env.FORGE_DIR  ?? path.resolve(ROOT, ".forge");
const CHARTER_PATH = path.resolve(ROOT, "PROJECT_CHARTER.md");
const SNAP_KEY   = process.env.FORGE_SNAPSHOT_KEY ?? "contextforge-default-key";

// ── DB helpers ──────────────────────────────────────────────────────────────

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

// ── Snapshot helpers (AES-256-GCM or base64 fallback) ──────────────────────

/** Derive 32-byte key from the FORGE_SNAPSHOT_KEY env var (SHA-256). */
function snapKey(): Buffer {
  return crypto.createHash("sha256").update(SNAP_KEY).digest();
}

/** Encrypt bytes: AES-256-GCM, 12-byte random nonce prepended. */
function encrypt(data: Buffer): Buffer {
  const key   = snapKey();
  const nonce = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", key, nonce);
  const ct     = Buffer.concat([cipher.update(data), cipher.final()]);
  const tag    = cipher.getAuthTag();          // 16 bytes
  // Format: nonce(12) | tag(16) | ciphertext
  return Buffer.concat([nonce, tag, ct]);
}

/** Decrypt bytes produced by encrypt(). Falls back to base64 if prefixed "B64:". */
function decrypt(data: Buffer): Buffer {
  const prefix = data.subarray(0, 4).toString("ascii");
  if (prefix === "B64:") {
    return Buffer.from(data.subarray(4).toString(), "base64");
  }
  const key    = snapKey();
  const nonce  = data.subarray(0, 12);
  const tag    = data.subarray(12, 28);
  const ct     = data.subarray(28);
  const decipher = crypto.createDecipheriv("aes-256-gcm", key, nonce);
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ct), decipher.final()]);
}

// ── Simple in-memory ZIP: write/read JSON bundles ──────────────────────────
// We build a minimal ZIP rather than pulling in a dependency.

interface ZipEntry { name: string; data: Buffer; }

async function buildZip(entries: ZipEntry[]): Promise<Buffer> {
  // Minimal ZIP: stored (no compression per entry for simplicity; outer encrypt provides compression)
  const localHeaders: Buffer[] = [];
  const centralDirs: Buffer[]  = [];
  let offset = 0;

  for (const entry of entries) {
    const nameBytes = Buffer.from(entry.name, "utf8");
    const compressedData = await deflate(entry.data);
    const crc  = crc32(entry.data);
    const localHeader = Buffer.alloc(30 + nameBytes.length);
    localHeader.writeUInt32LE(0x04034b50, 0);   // sig
    localHeader.writeUInt16LE(20, 4);            // version needed
    localHeader.writeUInt16LE(0, 6);             // flags
    localHeader.writeUInt16LE(8, 8);             // method: deflate
    localHeader.writeUInt16LE(0, 10);            // mod time
    localHeader.writeUInt16LE(0, 12);            // mod date
    localHeader.writeUInt32LE(crc, 14);
    localHeader.writeUInt32LE(compressedData.length, 18);
    localHeader.writeUInt32LE(entry.data.length, 22);
    localHeader.writeUInt16LE(nameBytes.length, 26);
    localHeader.writeUInt16LE(0, 28);
    nameBytes.copy(localHeader, 30);

    const centralDir = Buffer.alloc(46 + nameBytes.length);
    centralDir.writeUInt32LE(0x02014b50, 0);
    centralDir.writeUInt16LE(20, 4);
    centralDir.writeUInt16LE(20, 6);
    centralDir.writeUInt16LE(0, 8);
    centralDir.writeUInt16LE(8, 10);
    centralDir.writeUInt16LE(0, 12);
    centralDir.writeUInt16LE(0, 14);
    centralDir.writeUInt32LE(crc, 16);
    centralDir.writeUInt32LE(compressedData.length, 20);
    centralDir.writeUInt32LE(entry.data.length, 24);
    centralDir.writeUInt16LE(nameBytes.length, 28);
    centralDir.writeUInt16LE(0, 30);
    centralDir.writeUInt16LE(0, 32);
    centralDir.writeUInt16LE(0, 34);
    centralDir.writeUInt16LE(0, 36);
    centralDir.writeUInt32LE(0, 38);
    centralDir.writeUInt32LE(offset, 42);
    nameBytes.copy(centralDir, 46);

    localHeaders.push(localHeader, compressedData);
    centralDirs.push(centralDir);
    offset += localHeader.length + compressedData.length;
  }

  const centralStart = offset;
  const centralBuf   = Buffer.concat(centralDirs);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(0, 4);
  eocd.writeUInt16LE(0, 6);
  eocd.writeUInt16LE(entries.length, 8);
  eocd.writeUInt16LE(entries.length, 10);
  eocd.writeUInt32LE(centralBuf.length, 12);
  eocd.writeUInt32LE(centralStart, 16);
  eocd.writeUInt16LE(0, 20);

  return Buffer.concat([...localHeaders, centralBuf, eocd]);
}

/** CRC-32 (standard ZIP checksum). */
function crc32(buf: Buffer): number {
  const table = makeCrcTable();
  let crc = 0xffffffff;
  for (const byte of buf) {
    crc = (crc >>> 8) ^ table[(crc ^ byte) & 0xff]!;
  }
  return (crc ^ 0xffffffff) >>> 0;
}
function makeCrcTable(): Uint32Array {
  const t = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let j = 0; j < 8; j++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
    t[i] = c;
  }
  return t;
}

/**
 * Parse a .forge ZIP bundle back into a map of filename → Buffer.
 * Handles both STORE (method=0) and DEFLATE (method=8) entries.
 */
async function readZip(buf: Buffer): Promise<Map<string, Buffer>> {
  const result = new Map<string, Buffer>();
  let i = 0;

  while (i + 4 <= buf.length) {
    const sig = buf.readUInt32LE(i);
    if (sig === 0x04034b50) {
      // Local file header
      const method       = buf.readUInt16LE(i + 8);
      const compressedSz = buf.readUInt32LE(i + 18);
      const nameSz       = buf.readUInt16LE(i + 26);
      const extraSz      = buf.readUInt16LE(i + 28);
      const name         = buf.subarray(i + 30, i + 30 + nameSz).toString("utf8");
      const dataStart    = i + 30 + nameSz + extraSz;
      const compressed   = buf.subarray(dataStart, dataStart + compressedSz);

      if (method === 0) {
        result.set(name, compressed);
      } else if (method === 8) {
        result.set(name, await inflate(compressed) as Buffer);
      }
      i = dataStart + compressedSz;
    } else if (sig === 0x02014b50 || sig === 0x06054b50) {
      break; // central dir / EOCD — we're done
    } else {
      break;
    }
  }
  return result;
}

// ── MCP server ──────────────────────────────────────────────────────────────

const server = new McpServer({ name: "contextforge-nexus", version: "5.2.0" });

// ── list_projects ───────────────────────────────────────────────────────────

server.tool(
  "list_projects",
  "List all registered projects in this ContextForge instance, ordered by most recently created.",
  {},
  async () => {
    const db = openDb(true);
    try {
      const rows = db.prepare(
        "SELECT id, name, project_type, description, created_at FROM projects ORDER BY created_at DESC"
      ).all() as Record<string, unknown>[];
      return { content: [{ type: "text" as const, text: JSON.stringify({ projects: rows, count: rows.length }, null, 2) }] };
    } finally { db.close(); }
  }
);

// ── init_project ────────────────────────────────────────────────────────────

server.tool(
  "init_project",
  "Create or update a project. If project_id already exists, metadata is updated — no data is lost.",
  {
    project_id:   z.string().min(2).describe("Unique project slug (e.g. 'my-saas-app')"),
    name:         z.string().min(1).describe("Human-readable name"),
    project_type: z.enum(["code","research","study","general","custom"]).default("code"),
    description:  z.string().default(""),
    goals:        z.array(z.string()).default([]),
    tech_stack:   z.record(z.string()).default({}),
    constraints:  z.array(z.string()).default([]),
  },
  async ({ project_id, name, project_type, description, goals, tech_stack, constraints }) => {
    const db = openDb(false);
    try {
      db.prepare(`
        INSERT INTO projects (id, name, project_type, description, goals, tech_stack, constraints, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          name=excluded.name, project_type=excluded.project_type,
          description=excluded.description, goals=excluded.goals,
          tech_stack=excluded.tech_stack, constraints=excluded.constraints,
          updated_at=excluded.updated_at
      `).run(
        project_id, name, project_type, description,
        JSON.stringify(goals), JSON.stringify(tech_stack), JSON.stringify(constraints),
        nowIso(), nowIso(),
      );
      return { content: [{ type: "text" as const, text: JSON.stringify({ status: "ok", project_id, name }) }] };
    } finally { db.close(); }
  }
);

// ── rename_project ──────────────────────────────────────────────────────────

server.tool(
  "rename_project",
  "Rename a project display name. The project_id slug does not change.",
  {
    project_id:      z.string().min(2),
    new_name:        z.string().min(1),
    new_description: z.string().optional().describe("Omit to keep existing description"),
  },
  async ({ project_id, new_name, new_description }) => {
    const db = openDb(false);
    try {
      const exists = db.prepare("SELECT 1 FROM projects WHERE id = ?").get(project_id);
      if (!exists) return { content: [{ type: "text" as const, text: JSON.stringify({ error: `Project '${project_id}' not found` }) }] };
      if (new_description !== undefined) {
        db.prepare("UPDATE projects SET name = ?, description = ?, updated_at = ? WHERE id = ?")
          .run(new_name, new_description, nowIso(), project_id);
      } else {
        db.prepare("UPDATE projects SET name = ?, updated_at = ? WHERE id = ?")
          .run(new_name, nowIso(), project_id);
      }
      return { content: [{ type: "text" as const, text: JSON.stringify({ status: "renamed", project_id, new_name }) }] };
    } finally { db.close(); }
  }
);

// ── merge_projects ──────────────────────────────────────────────────────────

server.tool(
  "merge_projects",
  "Merge source_project_id INTO target_project_id. All nodes, tasks, and archives are re-assigned. Source is deleted. Irreversible — snapshot first.",
  {
    source_project_id: z.string().min(2).describe("Project to merge FROM (will be deleted)"),
    target_project_id: z.string().min(2).describe("Project to merge INTO (is kept)"),
  },
  async ({ source_project_id, target_project_id }) => {
    if (source_project_id === target_project_id) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ error: "source and target must be different projects" }) }] };
    }
    const db = openDb(false);
    try {
      const src = db.prepare("SELECT 1 FROM projects WHERE id = ?").get(source_project_id);
      const tgt = db.prepare("SELECT 1 FROM projects WHERE id = ?").get(target_project_id);
      if (!src) return { content: [{ type: "text" as const, text: JSON.stringify({ error: `Source project '${source_project_id}' not found` }) }] };
      if (!tgt) return { content: [{ type: "text" as const, text: JSON.stringify({ error: `Target project '${target_project_id}' not found` }) }] };

      const merge = db.transaction(() => {
        const nodes     = (db.prepare("UPDATE decision_nodes SET project_id = ? WHERE project_id = ?").run(target_project_id, source_project_id)).changes;
        const tasks     = (db.prepare("UPDATE tasks SET project_id = ? WHERE project_id = ?").run(target_project_id, source_project_id)).changes;
        const archived  = (db.prepare("UPDATE historical_nodes SET project_id = ? WHERE project_id = ?").run(target_project_id, source_project_id)).changes;
        db.prepare("DELETE FROM projects WHERE id = ?").run(source_project_id);
        return { nodes_moved: nodes, tasks_moved: tasks, archived_moved: archived };
      });

      const result = merge();
      return { content: [{ type: "text" as const, text: JSON.stringify({ status: "merged", source: source_project_id, target: target_project_id, ...result }) }] };
    } finally { db.close(); }
  }
);

// ── delete_project ──────────────────────────────────────────────────────────

server.tool(
  "delete_project",
  "Permanently delete a project. Active nodes are archived to historical_nodes first (unless archive_nodes=false). Irreversible.",
  {
    project_id:    z.string().min(2),
    archive_nodes: z.boolean().default(true).describe("Archive active nodes before deleting (recommended)"),
  },
  async ({ project_id, archive_nodes }) => {
    const db = openDb(false);
    try {
      const proj = db.prepare("SELECT 1 FROM projects WHERE id = ?").get(project_id);
      if (!proj) return { content: [{ type: "text" as const, text: JSON.stringify({ error: `Project '${project_id}' not found` }) }] };

      const del = db.transaction(() => {
        let archived = 0;
        if (archive_nodes) {
          const nodes = db.prepare(
            "SELECT * FROM decision_nodes WHERE project_id = ? AND tombstone = 0"
          ).all(project_id) as Record<string, unknown>[];

          for (const n of nodes) {
            db.prepare(`
              INSERT OR IGNORE INTO historical_nodes
                (id, original_id, project_id, summary, rationale, area, confidence,
                 status, created_by_agent, archived_by, archive_reason, original_created_at, type_metadata)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            `).run(
              crypto.randomUUID(), n.id as string, project_id,
              n.summary, n.rationale, n.area, n.confidence,
              n.status, n.created_by_agent, "mcp-delete",
              "project deleted via MCP", n.created_at, n.type_metadata ?? "{}",
            );
            archived++;
          }
        }
        const tasks  = (db.prepare("DELETE FROM tasks WHERE project_id = ?").run(project_id)).changes;
        db.prepare("DELETE FROM decision_nodes WHERE project_id = ?").run(project_id);
        db.prepare("DELETE FROM projects WHERE id = ?").run(project_id);
        return { archived_nodes: archived, tasks_deleted: tasks };
      });

      const result = del();
      return { content: [{ type: "text" as const, text: JSON.stringify({ status: "deleted", project_id, ...result }) }] };
    } finally { db.close(); }
  }
);

// ── project_stats ───────────────────────────────────────────────────────────

server.tool(
  "project_stats",
  "Return statistics for a project: node counts by area and status, task completion.",
  { project_id: z.string().min(2) },
  async ({ project_id }) => {
    const db = openDb(true);
    try {
      const proj = db.prepare("SELECT * FROM projects WHERE id = ?").get(project_id) as Record<string, unknown> | undefined;
      if (!proj) return { content: [{ type: "text" as const, text: JSON.stringify({ error: `Project '${project_id}' not found` }) }] };

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
        tasks: { ...taskCounts, total: totalTasks, pct_complete: totalTasks ? Math.round(((taskCounts["done"] ?? 0) / totalTasks) * 100) : 0 },
      }, null, 2) }] };
    } finally { db.close(); }
  }
);

// ── capture_decision ────────────────────────────────────────────────────────
// Note: The TypeScript server writes directly to SQLite. Charter guard
// (ReviewerGuard) runs only in the Python server. Use Python server if
// charter enforcement is required.

server.tool(
  "capture_decision",
  "Append a decision node to the knowledge graph. Records WHY a decision was made, alternatives considered, and causal dependencies.",
  {
    project_id:   z.string().min(2),
    summary:      z.string().min(1).describe("One-line decision summary"),
    rationale:    z.string().default("Rationale not explicitly stated."),
    area:         z.string().describe("e.g. auth, database, api-design, payments"),
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
        confidence, 0.5, JSON.stringify({}), "mcp-ts-client", 0,
        "mcp-ts-client", "active",
        JSON.stringify({ file_refs, packages: [] }),
        now, now,
      );
      return { content: [{ type: "text" as const, text: JSON.stringify({ status: "captured", node_id, project_id }) }] };
    } finally { db.close(); }
  }
);

// ── load_context ────────────────────────────────────────────────────────────

server.tool(
  "load_context",
  "Load hierarchical context for a project. L0=abstract metadata; L1=+decision summaries; L2=+full rationale+alternatives.",
  {
    project_id:   z.string().min(2),
    query:        z.string().optional().describe("Keyword filter on summary/area/rationale"),
    detail_level: z.enum(["L0","L1","L2"]).default("L1"),
    top_k:        z.number().int().default(10),
    area:         z.string().optional().describe("Filter by area"),
  },
  async ({ project_id, query, detail_level, top_k, area }) => {
    const db = openDb(true);
    try {
      const proj = db.prepare("SELECT * FROM projects WHERE id = ?").get(project_id) as Record<string, unknown> | undefined;
      if (!proj) return { content: [{ type: "text" as const, text: JSON.stringify({ error: `Project '${project_id}' not found. Call init_project first.` }) }] };

      const l0 = {
        level: "L0", project_id,
        name:        proj.name,
        type:        proj.project_type,
        description: proj.description,
        tech_stack:  parseJson(proj.tech_stack, {}),
        goals:       parseJson(proj.goals, []),
        constraints: parseJson(proj.constraints, []),
      };
      if (detail_level === "L0") return { content: [{ type: "text" as const, text: JSON.stringify(l0, null, 2) }] };

      // Build query dynamically
      const clauses: string[] = ["project_id = ?", "tombstone = 0", "status = 'active'"];
      const params: unknown[]  = [project_id];
      if (area) { clauses.push("area = ?"); params.push(area); }
      if (query) {
        const like = `%${query}%`;
        clauses.push("(summary LIKE ? OR area LIKE ? OR rationale LIKE ?)");
        params.push(like, like, like);
      }
      params.push(top_k);
      const rows = db.prepare(
        `SELECT * FROM decision_nodes WHERE ${clauses.join(" AND ")} ORDER BY importance DESC, created_at DESC LIMIT ?`
      ).all(...params) as Record<string, unknown>[];

      if (detail_level === "L1") {
        return { content: [{ type: "text" as const, text: JSON.stringify({
          ...l0, level: "L1",
          decisions: rows.map(r => ({ id: r.id, area: r.area, summary: r.summary, confidence: r.confidence })),
        }, null, 2) }] };
      }
      return { content: [{ type: "text" as const, text: JSON.stringify({
        ...l0, level: "L2",
        decisions: rows.map(r => ({
          id: r.id, area: r.area, summary: r.summary, rationale: r.rationale,
          alternatives:  parseJson(r.alternatives, []),
          dependencies:  parseJson(r.dependencies, []),
          confidence: r.confidence, status: r.status,
          created_by_agent: r.created_by_agent,
          type_metadata: parseJson(r.type_metadata, {}),
        })),
      }, null, 2) }] };
    } finally { db.close(); }
  }
);

// ── get_knowledge_node ──────────────────────────────────────────────────────

server.tool(
  "get_knowledge_node",
  "Retrieve a decision node by UUID or keyword search. Returns WHY the decision was made at the requested detail level.",
  {
    node_id:      z.string().optional().describe("UUID of the decision node (exact match)"),
    query:        z.string().optional().describe("Keyword search across summary/rationale/area"),
    project_id:   z.string().optional().describe("Scope keyword search to a project"),
    detail_level: z.enum(["L0","L1","L2"]).default("L1"),
    top_k:        z.number().int().default(5),
  },
  async ({ node_id, query, project_id, detail_level, top_k }) => {
    const db = openDb(true);
    try {
      let rows: Record<string, unknown>[];

      if (node_id) {
        const row = db.prepare("SELECT * FROM decision_nodes WHERE id = ? AND tombstone = 0").get(node_id) as Record<string, unknown> | undefined;
        if (!row) return { content: [{ type: "text" as const, text: JSON.stringify({ error: "node_not_found", node_id }) }] };
        rows = [row];
      } else if (query) {
        const like = `%${query}%`;
        const clauses = ["tombstone = 0", "(summary LIKE ? OR area LIKE ? OR rationale LIKE ?)"];
        const params: unknown[] = [like, like, like];
        if (project_id) { clauses.push("project_id = ?"); params.push(project_id); }
        params.push(top_k);
        rows = db.prepare(
          `SELECT * FROM decision_nodes WHERE ${clauses.join(" AND ")} ORDER BY importance DESC LIMIT ?`
        ).all(...params) as Record<string, unknown>[];
      } else {
        return { content: [{ type: "text" as const, text: JSON.stringify({ error: "Provide node_id or query" }) }] };
      }

      const format = (r: Record<string, unknown>) => {
        if (detail_level === "L0") return `[${r.area}] ${r.summary}`;
        if (detail_level === "L1") return { id: r.id, area: r.area, summary: r.summary, rationale: r.rationale, confidence: r.confidence, status: r.status };
        return {
          id: r.id, area: r.area, summary: r.summary, rationale: r.rationale,
          alternatives: parseJson(r.alternatives, []),
          dependencies: parseJson(r.dependencies, []),
          confidence: r.confidence, status: r.status,
          created_by_agent: r.created_by_agent, validated_by: r.validated_by,
          origin_client: r.origin_client, type_metadata: parseJson(r.type_metadata, {}),
        };
      };

      const result = rows.length === 1 ? format(rows[0]!) : rows.map(format);
      return { content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }] };
    } finally { db.close(); }
  }
);

// ── list_decisions ──────────────────────────────────────────────────────────

server.tool(
  "list_decisions",
  "List decision nodes for a project with optional area and status filters.",
  {
    project_id: z.string().min(2),
    area:       z.string().optional().describe("Filter by area (e.g. 'auth', 'database')"),
    status:     z.enum(["active","deprecated","quarantined","pending"]).default("active"),
    limit:      z.number().int().default(20),
  },
  async ({ project_id, area, status, limit }) => {
    const db = openDb(true);
    try {
      const clauses = ["project_id = ?", "tombstone = 0", "status = ?"];
      const params: unknown[] = [project_id, status];
      if (area) { clauses.push("area = ?"); params.push(area); }
      params.push(limit);
      const rows = db.prepare(
        `SELECT id, area, summary, rationale, confidence, status, created_at FROM decision_nodes WHERE ${clauses.join(" AND ")} ORDER BY importance DESC LIMIT ?`
      ).all(...params) as Record<string, unknown>[];
      return { content: [{ type: "text" as const, text: JSON.stringify({ project_id, count: rows.length, decisions: rows }, null, 2) }] };
    } finally { db.close(); }
  }
);

// ── update_decision ─────────────────────────────────────────────────────────

server.tool(
  "update_decision",
  "Edit fields on an existing decision node (summary, rationale, area, confidence, importance).",
  {
    node_id:    z.string().describe("UUID of the decision node"),
    summary:    z.string().optional(),
    rationale:  z.string().optional(),
    area:       z.string().optional(),
    confidence: z.number().min(0).max(1).optional(),
    importance: z.number().min(0).max(1).optional(),
  },
  async ({ node_id, ...fields }) => {
    const db = openDb(false);
    try {
      const allowed = ["summary", "rationale", "area", "confidence", "importance"] as const;
      const updates = allowed.filter(k => fields[k] !== undefined);
      if (updates.length === 0) {
        return { content: [{ type: "text" as const, text: JSON.stringify({ error: "No updatable fields provided (allowed: summary, rationale, area, confidence, importance)" }) }] };
      }
      const setClauses = updates.map(k => `${k} = ?`).join(", ") + ", updated_at = ?";
      const params     = [...updates.map(k => fields[k]), nowIso(), node_id];
      const result     = db.prepare(`UPDATE decision_nodes SET ${setClauses} WHERE id = ? AND tombstone = 0`).run(...params);
      if (result.changes === 0) return { content: [{ type: "text" as const, text: JSON.stringify({ error: `Decision node '${node_id}' not found` }) }] };
      return { content: [{ type: "text" as const, text: JSON.stringify({ status: "updated", node_id, updated_fields: updates }) }] };
    } finally { db.close(); }
  }
);

// ── deprecate_decision ──────────────────────────────────────────────────────

server.tool(
  "deprecate_decision",
  "Mark a decision node as deprecated with a reason and optional replacement node ID.",
  {
    node_id:        z.string().describe("UUID of the decision to deprecate"),
    reason:         z.string().min(1).describe("Why this decision is being deprecated"),
    replacement_id: z.string().optional().describe("UUID of the replacement decision node"),
  },
  async ({ node_id, reason, replacement_id }) => {
    const db = openDb(false);
    try {
      let result: Database.RunResult;
      if (replacement_id) {
        result = db.prepare(
          "UPDATE decision_nodes SET status = 'deprecated', deprecated_reason = ?, replacement_id = ?, updated_at = ? WHERE id = ? AND tombstone = 0"
        ).run(reason, replacement_id, nowIso(), node_id);
      } else {
        result = db.prepare(
          "UPDATE decision_nodes SET status = 'deprecated', deprecated_reason = ?, updated_at = ? WHERE id = ? AND tombstone = 0"
        ).run(reason, nowIso(), node_id);
      }
      if (result.changes === 0) return { content: [{ type: "text" as const, text: JSON.stringify({ error: `Decision node '${node_id}' not found` }) }] };
      return { content: [{ type: "text" as const, text: JSON.stringify({ status: "deprecated", node_id, reason, replacement_id: replacement_id ?? null }) }] };
    } finally { db.close(); }
  }
);

// ── link_decisions ──────────────────────────────────────────────────────────

server.tool(
  "link_decisions",
  "Create a typed edge between two decision nodes (depends_on | replaces | contradicts | refines | implements).",
  {
    source_id: z.string().describe("UUID of the source decision"),
    target_id: z.string().describe("UUID of the target decision"),
    edge_type: z.enum(["depends_on","replaces","contradicts","refines","implements"]),
  },
  async ({ source_id, target_id, edge_type }) => {
    const db = openDb(false);
    try {
      const edge_id = crypto.randomUUID();
      db.prepare(
        "INSERT INTO decision_edges (id, source_id, target_id, edge_type) VALUES (?, ?, ?, ?)"
      ).run(edge_id, source_id, target_id, edge_type);
      return { content: [{ type: "text" as const, text: JSON.stringify({ status: "linked", edge_id, source: source_id, target: target_id, type: edge_type }) }] };
    } finally { db.close(); }
  }
);

// ── list_tasks ──────────────────────────────────────────────────────────────

server.tool(
  "list_tasks",
  "List tasks for a project with optional status filter.",
  {
    project_id: z.string().min(2),
    status:     z.enum(["pending","in_progress","done","blocked"]).optional(),
    limit:      z.number().int().default(20),
  },
  async ({ project_id, status, limit }) => {
    const db = openDb(true);
    try {
      const rows = status
        ? db.prepare("SELECT * FROM tasks WHERE project_id = ? AND status = ? ORDER BY priority ASC, created_at ASC LIMIT ?").all(project_id, status, limit)
        : db.prepare("SELECT * FROM tasks WHERE project_id = ? ORDER BY priority ASC, created_at ASC LIMIT ?").all(project_id, limit);
      return { content: [{ type: "text" as const, text: JSON.stringify({ project_id, count: rows.length, tasks: rows }, null, 2) }] };
    } finally { db.close(); }
  }
);

// ── create_task ─────────────────────────────────────────────────────────────

server.tool(
  "create_task",
  "Create a new task in a project. Auto-creates a stub project if project_id does not exist.",
  {
    project_id:  z.string().min(2),
    title:       z.string().min(1),
    description: z.string().default(""),
    priority:    z.number().int().min(1).max(5).default(3).describe("1=highest, 5=lowest"),
    sprint:      z.string().default(""),
    assigned_to: z.string().default(""),
  },
  async ({ project_id, title, description, priority, sprint, assigned_to }) => {
    const db = openDb(false);
    try {
      db.prepare("INSERT OR IGNORE INTO projects (id, name, project_type) VALUES (?, ?, 'general')").run(project_id, project_id);
      const task_id = crypto.randomUUID();
      const now = nowIso();
      db.prepare(
        "INSERT INTO tasks (id, project_id, title, description, status, priority, sprint, assigned_to, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
      ).run(task_id, project_id, title, description, "pending", priority, sprint, assigned_to, now, now);
      return { content: [{ type: "text" as const, text: JSON.stringify({ status: "created", task_id, title }) }] };
    } finally { db.close(); }
  }
);

// ── update_task ─────────────────────────────────────────────────────────────

server.tool(
  "update_task",
  "Update the status of an existing task.",
  {
    task_id: z.string(),
    status:  z.enum(["pending","in_progress","done","blocked"]),
  },
  async ({ task_id, status }) => {
    const db = openDb(false);
    try {
      const result = db.prepare("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?").run(status, nowIso(), task_id);
      if (result.changes === 0) return { content: [{ type: "text" as const, text: JSON.stringify({ error: `Task '${task_id}' not found` }) }] };
      return { content: [{ type: "text" as const, text: JSON.stringify({ status: "updated", task_id, new_status: status }) }] };
    } finally { db.close(); }
  }
);

// ── rollback ────────────────────────────────────────────────────────────────

server.tool(
  "rollback",
  "Time-travel undo: mark all events after a given event_id or timestamp as rolled_back. Returns the count of pruned events.",
  {
    event_id:   z.string().optional().describe("Roll back to just before this event (exclusive)"),
    timestamp:  z.string().optional().describe("ISO-8601 timestamp — roll back everything after this point"),
    project_id: z.string().optional().describe("Scope rollback to a single project's events"),
  },
  async ({ event_id, timestamp, project_id }) => {
    if (!event_id && !timestamp) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ error: "Provide either event_id or timestamp" }) }] };
    }
    const db = openDb(false);
    try {
      let cutoff: string;
      if (event_id) {
        const row = db.prepare("SELECT created_at FROM events WHERE event_id = ?").get(event_id) as { created_at: string } | undefined;
        if (!row) return { content: [{ type: "text" as const, text: JSON.stringify({ error: `event_id '${event_id}' not found` }) }] };
        cutoff = row.created_at;
      } else {
        cutoff = timestamp!;
      }

      let result: Database.RunResult;
      if (project_id) {
        result = db.prepare(
          "UPDATE events SET status = 'rolled_back' WHERE created_at > ? AND status = 'active' AND (project_id = ? OR project_id IS NULL)"
        ).run(cutoff, project_id);
      } else {
        result = db.prepare(
          "UPDATE events SET status = 'rolled_back' WHERE created_at > ? AND status = 'active'"
        ).run(cutoff);
      }

      // Insert a ROLLBACK event to record the operation
      const rollback_id = crypto.randomUUID();
      db.prepare(
        "INSERT INTO events (event_id, event_type, content, status, created_at, project_id) VALUES (?,?,?,?,?,?)"
      ).run(
        rollback_id, "ROLLBACK",
        JSON.stringify({ pruned: result.changes, cutoff, by: "mcp-ts-client" }),
        "active", nowIso(), project_id ?? null,
      );

      return { content: [{ type: "text" as const, text: JSON.stringify({ pruned_events: result.changes, status: "rolled_back", cutoff }) }] };
    } finally { db.close(); }
  }
);

// ── snapshot ────────────────────────────────────────────────────────────────

server.tool(
  "snapshot",
  "Create an AES-256-GCM encrypted .forge snapshot of the event ledger. Compatible with Python FluidSync format.",
  {
    label: z.string().default("manual").describe("Human-readable label for this snapshot"),
  },
  async ({ label }) => {
    try {
      fs.mkdirSync(FORGE_DIR, { recursive: true });

      // Read all active events from ledger
      const db = openDb(true);
      const events = db.prepare(
        "SELECT * FROM events WHERE status = 'active' ORDER BY created_at ASC"
      ).all() as Record<string, unknown>[];
      db.close();

      const eventsJson = Buffer.from(JSON.stringify(events, null, 2), "utf8");

      // Read charter if present
      let charterBytes = Buffer.alloc(0);
      try { charterBytes = fs.readFileSync(CHARTER_PATH); } catch { /* ok */ }

      const checksum = crypto.createHash("sha256").update(eventsJson).digest("hex");
      const manifest = {
        version: "5.0", label,
        created_at: new Date().toISOString(),
        event_count: events.length, checksum,
        created_by: "mcp-ts-server",
      };
      const manifestJson = Buffer.from(JSON.stringify(manifest, null, 2), "utf8");

      // Build ZIP and encrypt
      const zipBuf = await buildZip([
        { name: "events.json",   data: eventsJson },
        { name: "charter.md",    data: charterBytes },
        { name: "manifest.json", data: manifestJson },
      ]);
      const encrypted = encrypt(zipBuf);

      const ts = new Date().toISOString().replace(/[:.]/g, "").slice(0, 15);
      const safe_label = label.replace(/\s+/g, "_").slice(0, 32);
      const snapPath = path.join(FORGE_DIR, `snapshot_${ts}_${safe_label}.forge`);
      fs.writeFileSync(snapPath, encrypted);

      return { content: [{ type: "text" as const, text: JSON.stringify({ status: "ok", snapshot_path: snapPath, event_count: events.length, size_bytes: encrypted.length }) }] };
    } catch (e: unknown) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ error: String(e) }) }] };
    }
  }
);

// ── list_snapshots ──────────────────────────────────────────────────────────

server.tool(
  "list_snapshots",
  "List all .forge snapshot files in the .forge/ directory with metadata.",
  {},
  async () => {
    try {
      fs.mkdirSync(FORGE_DIR, { recursive: true });
      const files = fs.readdirSync(FORGE_DIR)
        .filter(f => f.endsWith(".forge"))
        .sort()
        .map(f => {
          const fp   = path.join(FORGE_DIR, f);
          const stat = fs.statSync(fp);
          return { name: f, path: fp, size_bytes: stat.size, modified_at: stat.mtime.toISOString() };
        });
      return { content: [{ type: "text" as const, text: JSON.stringify({ snapshots: files, count: files.length }, null, 2) }] };
    } catch (e: unknown) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ error: String(e) }) }] };
    }
  }
);

// ── replay_sync ─────────────────────────────────────────────────────────────

server.tool(
  "replay_sync",
  "Restore events from a .forge snapshot. Events already in the ledger are skipped (idempotent). Returns count of replayed events.",
  {
    forge_path: z.string().describe("Path to the .forge snapshot file"),
  },
  async ({ forge_path }) => {
    try {
      if (!fs.existsSync(forge_path)) {
        return { content: [{ type: "text" as const, text: JSON.stringify({ error: `.forge file not found: ${forge_path}` }) }] };
      }
      const raw    = fs.readFileSync(forge_path);
      const zipBuf = decrypt(raw);
      const entries = await readZip(zipBuf);

      const eventsRaw = entries.get("events.json");
      if (!eventsRaw) return { content: [{ type: "text" as const, text: JSON.stringify({ error: "Invalid .forge file: missing events.json" }) }] };

      const events: Record<string, unknown>[] = JSON.parse(eventsRaw.toString("utf8"));

      // Optionally restore charter
      const charterBytes = entries.get("charter.md");
      if (charterBytes && charterBytes.length > 0 && !fs.existsSync(CHARTER_PATH)) {
        fs.writeFileSync(CHARTER_PATH, charterBytes);
      }

      const db = openDb(false);
      try {
        const existing = new Set(
          (db.prepare("SELECT event_id FROM events").all() as { event_id: string }[]).map(r => r.event_id)
        );

        let replayed = 0;
        const insert = db.prepare(
          "INSERT OR IGNORE INTO events (event_id, parent_id, event_type, content, metadata, status, created_at, prev_hash, project_id) VALUES (?,?,?,?,?,?,?,?,?)"
        );

        // oldest-first (events.json is newest-first from Python export)
        const ordered = [...events].reverse();
        const replay  = db.transaction(() => {
          for (const evt of ordered) {
            const eid = evt["event_id"] as string;
            if (existing.has(eid)) continue;
            const content = typeof evt["content"] === "string" ? evt["content"] : JSON.stringify(evt["content"]);
            insert.run(
              eid, evt["parent_id"] ?? null,
              evt["event_type"] ?? "AGENT_THOUGHT",
              content,
              typeof evt["metadata"] === "string" ? evt["metadata"] : JSON.stringify(evt["metadata"] ?? {}),
              "active",
              evt["created_at"] ?? nowIso(),
              evt["prev_hash"] ?? null,
              evt["project_id"] ?? null,
            );
            replayed++;
          }
          return replayed;
        });

        const count = replay();
        return { content: [{ type: "text" as const, text: JSON.stringify({ status: "synced", replayed_events: count, total_in_snapshot: events.length }) }] };
      } finally { db.close(); }
    } catch (e: unknown) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ error: String(e) }) }] };
    }
  }
);

// ── list_events ─────────────────────────────────────────────────────────────

server.tool(
  "list_events",
  "Inspect the event ledger — append-only record of all agent activity.",
  {
    last_n:     z.number().int().default(20).describe("Number of recent events to return"),
    event_type: z.string().optional().describe("Filter by event type e.g. AGENT_THOUGHT, FILE_DIFF, CHECKPOINT"),
    project_id: z.string().optional().describe("Filter by project"),
  },
  async ({ last_n, event_type, project_id }) => {
    const db = openDb(true);
    try {
      const clauses: string[] = [];
      const params: unknown[] = [];
      if (event_type) { clauses.push("event_type = ?"); params.push(event_type); }
      if (project_id) { clauses.push("project_id = ?"); params.push(project_id); }
      params.push(last_n);
      const where = clauses.length ? `WHERE ${clauses.join(" AND ")}` : "";
      const rows = db.prepare(
        `SELECT event_id, event_type, status, created_at, project_id, content FROM events ${where} ORDER BY created_at DESC LIMIT ?`
      ).all(...params) as Record<string, unknown>[];

      const events = rows.map(r => ({
        event_id: r.event_id, event_type: r.event_type,
        status: r.status, created_at: r.created_at,
        project_id: r.project_id,
        content: parseJson(r.content, r.content),
      }));
      return { content: [{ type: "text" as const, text: JSON.stringify(events, null, 2) }] };
    } finally { db.close(); }
  }
);

// ── search_context ──────────────────────────────────────────────────────────

server.tool(
  "search_context",
  "Keyword search over local project files. Searches file contents using fuzzy word matching. For full semantic search, use the Python server.",
  {
    query:     z.string().min(1).describe("Search query"),
    top_k:     z.number().int().default(5).describe("Max results to return"),
    directory: z.string().optional().describe("Directory to search (defaults to ContextForge root)"),
    extensions: z.array(z.string()).default([".py",".ts",".md",".json",".yaml",".yml",".toml"]).describe("File extensions to include"),
  },
  async ({ query, top_k, directory, extensions }) => {
    const searchRoot = directory ?? ROOT;
    const SKIP_DIRS  = new Set(["node_modules", ".git", "__pycache__", ".forge", "dist", "data", "papers", "benchmark"]);

    const terms = query.toLowerCase().split(/\s+/).filter(t => t.length > 2);
    if (terms.length === 0) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ error: "Query too short — use at least one word with 3+ characters" }) }] };
    }

    const results: Array<{ file: string; line: number; text: string; score: number }> = [];

    function crawl(dir: string): void {
      let entries: fs.Dirent[];
      try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
      for (const entry of entries) {
        if (entry.isDirectory()) {
          if (!SKIP_DIRS.has(entry.name)) crawl(path.join(dir, entry.name));
        } else if (entry.isFile() && extensions.some(ext => entry.name.endsWith(ext))) {
          const fp = path.join(dir, entry.name);
          let content: string;
          try { content = fs.readFileSync(fp, "utf8"); } catch { continue; }
          const lines = content.split("\n");
          for (let i = 0; i < lines.length; i++) {
            const line = lines[i]!;
            const lower = line.toLowerCase();
            const hits  = terms.filter(t => lower.includes(t)).length;
            if (hits > 0) {
              results.push({
                file: path.relative(ROOT, fp),
                line: i + 1,
                text: line.trim().slice(0, 200),
                score: hits / terms.length,
              });
            }
          }
        }
      }
    }

    crawl(searchRoot);
    results.sort((a, b) => b.score - a.score || a.file.localeCompare(b.file));
    const top = results.slice(0, top_k);

    return { content: [{ type: "text" as const, text: JSON.stringify({ query, count: top.length, results: top }, null, 2) }] };
  }
);

// ── Start ───────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  process.stderr.write(`ContextForge Nexus MCP v5.2.0 (TypeScript, 22 tools) — db=${DB_PATH}\n`);
}

main().catch(err => { process.stderr.write(`Fatal: ${err}\n`); process.exit(1); });
