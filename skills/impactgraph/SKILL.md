---
name: impactgraph
description: Change-impact analysis for code and data. Use before merging or when asked "what breaks if I change this?" — runs impactgraph on the current git diff (or a named file/function/dbt model/table/column) and returns the blast radius across Python code, dbt models, warehouse tables, columns and dashboards, with a risk level, owners to notify and a test plan. Triggers on: "impact", "what depends on", "what breaks", "blast radius", "is this change safe", "downstream", "lineage", "before I merge".
---

# impactgraph — is this change safe?

impactgraph builds a deterministic graph (Python AST, dbt manifest, SQL lineage, git diff,
OpenLineage / DataHub lineage, warehouse information_schema) and computes what a change can break.
The graph is never built by an LLM; you only explain its output.

## When to use
- Before merging a change that touches data models, SQL, or code that feeds data.
- When the user asks what depends on / what breaks if / how risky a change is.
- On request for a blast radius, downstream list, or who to notify.

## Steps
1. Make sure a graph exists (skip if `impactgraph.json` is present and fresh):
   ```bash
   impactgraph build --repo . --dbt-manifest target/manifest.json --sql sql -o impactgraph.json --update
   ```
   Add `--openlineage events.json` or `--lineage-file lineage.yml` if the repo has them.
2. For the current change:
   ```bash
   impactgraph diff --repo . --graph impactgraph.json --json
   ```
   For a named thing (dbt model, table, column, function, file):
   ```bash
   impactgraph impact dbt:customer --graph impactgraph.json --json
   impactgraph impact column:dim_customer.customer_key --graph impactgraph.json --json
   ```
   Useful extras: `--no-inferred` (only artifact-backed edges), `paths A B`, `hotspots`,
   `html NODE -o impact.html` for an interactive view.
3. Read the JSON: `risk.level`, `affected_by_type`, `owners`, `recommended_tests`, and `trees`
   (each child has `via` = edge type and `provenance` = extracted|inferred).
4. Report to the user: what changed, what can break and why (walk the tree), whether the risk
   level looks right, who to notify, and the tests to run before and after deploy. Mark anything
   that came via an `inferred` edge as a heuristic. Do not invent nodes that are not in the output.

## Notes
- Node ids: `file:path`, `func:path::name`, `dbt:model`, `source:src.name`, `table:db.schema.name`,
  `column:model.col`, `exposure:name`, `job:namespace/name`.
- If the graph is missing, run step 1; if a reference is ambiguous the CLI lists candidates.
- `impactgraph mcp --graph impactgraph.json` exposes the same as MCP tools (impact, diff, find_nodes, paths, hotspots).
