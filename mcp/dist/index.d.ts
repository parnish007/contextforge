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
export {};
