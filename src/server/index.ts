/**
 * ContextForge v3.0 — MCP Server (Phase 1 Skeleton)
 *
 * Implements the Model Context Protocol server that exposes the Shared
 * Knowledge Graph to IDE clients (Claude.ai, Claude Code, Cursor).
 *
 * Phase 1 tools implemented here:
 *   • get_knowledge_node   — fetch a single decision node by ID
 *
 * Tools planned for Phase 1 completion (stubs below):
 *   • init_project         — create / register a new project
 *   • capture_decision     — trigger the Ghost-Coder pipeline
 *   • load_context         — hierarchical context assembly (L0/L1/L2)
 *   • agent_status         — health summary of the 8-agent pool
 *
 * Spec reference: OMEGA_SPEC.md §14 (folder structure) and §3 (interaction flow).
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import Database from "better-sqlite3";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { z } from "zod";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// Database lives two levels up from src/server/ → project root / data /
const DB_PATH =
  process.env.DB_PATH ??
  path.resolve(__dirname, "../../data/contextforge.db");

// ---------------------------------------------------------------------------
// Database helper
// ---------------------------------------------------------------------------

function openDb(): Database.Database {
  const db = new Database(DB_PATH, { readonly: true });
  db.pragma("journal_mode = WAL");
  return db;
}

/** Parse JSON columns safely; return the raw string on failure. */
function parseJsonColumns(
  row: Record<string, unknown>,
  columns: string[]
): Record<string, unknown> {
  const result = { ...row };
  for (const col of columns) {
    if (typeof result[col] === "string") {
      try {
        result[col] = JSON.parse(result[col] as string);
      } catch {
        // leave as-is
      }
    }
  }
  return result;
}

// ---------------------------------------------------------------------------
// MCP Server
// ---------------------------------------------------------------------------

const server = new McpServer({
  name: "contextforge",
  version: "0.1.0",
});

// ---------------------------------------------------------------------------
// Tool: get_knowledge_node
// ---------------------------------------------------------------------------

server.tool(
  "get_knowledge_node",
  "Retrieve a single decision node from the ContextForge knowledge graph by its ID. Returns the full node including summary, rationale, alternatives considered, confidence score, and causal dependencies.",
  {
    node_id: z
      .string()
      .uuid()
      .describe("UUID of the decision node to retrieve"),
    detail_level: z
      .enum(["L0", "L1", "L2"])
      .default("L1")
      .describe(
        "L0 = one-line summary only; L1 = summary + rationale + confidence; L2 = full provenance"
      ),
  },
  async ({ node_id, detail_level }) => {
    const db = openDb();
    try {
      const row = db
        .prepare(
          `SELECT * FROM decision_nodes
           WHERE id = ? AND tombstone = FALSE`
        )
        .get(node_id) as Record<string, unknown> | undefined;

      if (!row) {
        return {
          content: [
            {
              type: "text" as const,
              text: JSON.stringify({
                error: "node_not_found",
                node_id,
              }),
            },
          ],
        };
      }

      const node = parseJsonColumns(row, [
        "alternatives",
        "dependencies",
        "vclock",
        "type_metadata",
      ]);

      // Apply detail-level projection (mirrors DecisionNode.to_context_string)
      let text: string;
      if (detail_level === "L0") {
        text = `[${node.area}] ${node.summary}`;
      } else if (detail_level === "L1") {
        text = [
          `[${node.area}] ${node.summary}`,
          `Rationale: ${node.rationale ?? "—"}`,
          `Confidence: ${(node.confidence as number).toFixed(2)}`,
          `Status: ${node.status}`,
        ].join("\n");
      } else {
        // L2 — full provenance
        const alts = Array.isArray(node.alternatives)
          ? (node.alternatives as Array<{ option?: string }>)
              .map((a) => a.option ?? "?")
              .join(", ")
          : "—";
        const deps = Array.isArray(node.dependencies)
          ? (node.dependencies as string[]).slice(0, 5).join(", ")
          : "—";
        text = [
          `[${node.area}] ${node.summary}`,
          `Rationale: ${node.rationale ?? "—"}`,
          `Alternatives considered: ${alts}`,
          `Dependencies: ${deps}`,
          `Created by: ${node.created_by_agent} | Validated: ${node.validated_by}`,
          `Confidence: ${(node.confidence as number).toFixed(2)} | Importance: ${(node.importance as number).toFixed(2)}`,
          `Status: ${node.status} | Origin: ${node.origin_client ?? "—"}`,
        ].join("\n");
      }

      return {
        content: [{ type: "text" as const, text }],
      };
    } finally {
      db.close();
    }
  }
);

// ---------------------------------------------------------------------------
// Tool stub: init_project (Phase 1 — coming next)
// ---------------------------------------------------------------------------

server.tool(
  "init_project",
  "[STUB] Register a new project with the ContextForge knowledge graph. Full implementation in Phase 1.",
  {
    project_id: z.string().describe("Unique identifier for the project"),
    name: z.string().describe("Human-readable project name"),
    project_type: z
      .enum(["code", "research", "study", "general", "custom"])
      .default("code"),
  },
  async ({ project_id, name, project_type }) => {
    return {
      content: [
        {
          type: "text" as const,
          text: JSON.stringify({
            status: "stub",
            message:
              "init_project is a Phase 1 stub. Full implementation pending StorageAdapter write path.",
            project_id,
            name,
            project_type,
          }),
        },
      ],
    };
  }
);

// ---------------------------------------------------------------------------
// Tool stub: load_context (Phase 1 — coming next)
// ---------------------------------------------------------------------------

server.tool(
  "load_context",
  "[STUB] Load hierarchical context (L0/L1/L2) for a project or topic. Full implementation in Phase 1.",
  {
    project_id: z.string().describe("Project to load context for"),
    query: z.string().optional().describe("Specific topic or area to focus on"),
    detail_level: z.enum(["L0", "L1", "L2"]).default("L1"),
    model_context_window: z
      .number()
      .int()
      .positive()
      .optional()
      .describe(
        "Model context window in tokens (e.g. 128000 for GPT-4o, 1000000 for Gemini 1.5 Pro). " +
        "When provided with CONTEXT_BUDGET_MODE=model_aware or adaptive, the DCI token budget " +
        "is set to min(0.25 × model_context_window, 8000) instead of the fixed default B=1500."
      ),
  },
  async ({ project_id, query, detail_level, model_context_window }) => {
    return {
      content: [
        {
          type: "text" as const,
          text: JSON.stringify({
            status: "stub",
            message:
              "load_context is a Phase 1 stub. Full implementation pending Librarian integration.",
            project_id,
            query,
            detail_level,
            model_context_window: model_context_window ?? null,
            dci_note:
              model_context_window != null
                ? `DCI budget will be min(0.25 × ${model_context_window}, 8000) = ` +
                  `${Math.min(Math.floor(0.25 * model_context_window), 8000)} tokens ` +
                  `when CONTEXT_BUDGET_MODE=adaptive or model_aware`
                : "Using default B=1500 (fixed mode). Set CONTEXT_BUDGET_MODE=adaptive to scale with model.",
          }),
        },
      ],
    };
  }
);

// ---------------------------------------------------------------------------
// Start server
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  // MCP servers communicate over stdio; log to stderr to avoid polluting the
  // JSON-RPC stream.
  process.stderr.write(
    `ContextForge MCP server v0.1.0 started — db=${DB_PATH}\n`
  );
}

main().catch((err) => {
  process.stderr.write(`Fatal: ${err}\n`);
  process.exit(1);
});
