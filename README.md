# impactgraph

**AI-powered Change Impact Graph for data and code systems.**

Answer the question every data platform engineer asks before merging:

> *"If I change this file, function, dbt model, SQL column, or table — what can break?"*

Projects like Graphify / Code-Graph-RAG build graphs from source code, and DataHub / OpenLineage focus on data lineage. `impactgraph` connects both worlds into a **single unified graph**:

```
Git Change → Python Function → API → Table → dbt Model → Report / Dashboard
```

## The key idea

**The graph is never built by an LLM.** It is constructed deterministically from real engineering artifacts:

| Artifact | Extractor | What it contributes |
|---|---|---|
| Python source | `ast` | files, functions, classes, imports, call edges (calls are tagged *inferred*) |
| dbt project | `manifest.json` | models, sources, seeds, exposures, the resolved DAG, materialized tables, columns, **owners**, and — from `compiled_code` — **column-to-column lineage** |
| Raw SQL | `sqlglot` | table/view lineage **and column-level lineage** (through aliases, CTEs and renames) |
| Git | `git diff` | which files *and which functions* actually changed |
| OpenLineage events | JSON / NDJSON | datasets, jobs, schema and `columnLineage` facets, ownership — lineage Airflow / Marquez / DataHub already observed |
| DataHub lineage file | YAML / JSON | curated dataset lineage + fine-grained (column) lineage |
| Warehouse `information_schema` | any DB-API connection | real tables, columns (with types) and view lineage; diff two snapshots for schema drift |

Graph algorithms compute the blast radius, a deterministic risk score, owners to notify, and a test plan. The AI layer (optional) only **explains** the already-computed analysis — so you get LLM readability with graph-level trust.

## Install

```bash
pip install impactgraph            # core (Python + dbt + git + OpenLineage + lineage-file + warehouse)
pip install impactgraph[sql]       # + SQL / column lineage via sqlglot
pip install impactgraph[ai]        # + AI explanations via the Claude API
pip install impactgraph[mcp]       # + MCP server for Claude Code / Cursor / Codex
pip install impactgraph[all]       # everything (also PyYAML for YAML lineage files)
```

## Quick start (CLI)

```bash
# 1. Build the unified graph from whatever artifacts you have
impactgraph build --repo ./src --dbt-manifest target/manifest.json --sql ./sql \
                  --openlineage events.ndjson --lineage-file lineage.yml -o impactgraph.json

# 2. What breaks if I change this dbt model / table / column / function?
impactgraph impact dbt:customer
impactgraph impact column:dim_customer.customer_key

# 3. What breaks given my current (uncommitted) git diff?  — the CI command
impactgraph diff --repo . --graph impactgraph.json

# 4. Explore
impactgraph nodes --search customer --type dbt_model
impactgraph paths dbt:customer exposure:revenue_report
impactgraph hotspots --top 10                    # where a change hurts most
impactgraph html dbt:customer -o impact.html     # interactive blast-radius view
impactgraph export --format graphml -o g.graphml # also dot | cypher | json
impactgraph graph-diff old.json new.json         # schema / dependency drift

# 5. Keep it fresh
impactgraph build ... --update                   # skips when inputs unchanged
impactgraph watch --repo ./src --dbt-manifest target/manifest.json
impactgraph hook-install --git-repo . --repo ./src --dbt-manifest target/manifest.json

# 6. AI explanation / AI assistants
impactgraph explain dbt:customer                 # needs [ai] + ANTHROPIC_API_KEY
impactgraph mcp --graph impactgraph.json         # MCP server (stdio), needs [mcp]
```

Example output:

```
⚠ Change Impact

Changed:
  customer

Risk: HIGH  (score 24.5)

⬢ customer (dbt_model)
├── ⬢ dim_customer (dbt_model) via depends_on
│   └── ⬢ fact_booking (dbt_model) via depends_on
│       ├── 📊 revenue_report (dashboard) via exposes
│       └── 📊 customer_dashboard (dashboard) via exposes
└── ▤ prod.analytics.customer (view) via writes_to

Affected:
  3 dbt model(s)
  2 dashboard(s)
  2 table(s)

Notify (owners of affected artifacts):
  finance: revenue_report
  growth: customer_dashboard

Recommended tests:
  ✓ dbt build --select customer+ dim_customer+ fact_booking+
  ✓ Run a schema/contract check on prod.analytics.fact_booking
  ✓ Manually validate 'revenue_report' after deploy (numbers & filters)
```

Add `--json` for machine output, `--no-inferred` to keep only artifact-backed edges, `--html out.html` for the interactive view.

## Quick start (Python API)

```python
from impactgraph import ImpactGraph, PythonExtractor, DbtExtractor, analyze_impact

graph = ImpactGraph()
graph.merge(PythonExtractor("./src").extract())
graph.merge(DbtExtractor("target/manifest.json").extract())

analysis = analyze_impact(graph, ["dbt:customer"])
print(analysis.risk)               # {'score': 24.5, 'level': 'HIGH'}
print(analysis.affected)           # {node_id: depth, ...}
print(analysis.owners)             # {'finance': ['revenue_report'], ...}
print(analysis.recommended_tests)

# More extractors
from impactgraph import OpenLineageExtractor, LineageFileExtractor, WarehouseExtractor, SqlExtractor
graph.merge(OpenLineageExtractor("events.ndjson").extract())
graph.merge(LineageFileExtractor("lineage.yml").extract())
graph.merge(WarehouseExtractor(conn, database="PROD", schemas=["ANALYTICS"], dialect="snowflake").extract())

# Optional AI explanation (pip install impactgraph[ai])
from impactgraph.ai import explain_impact
print(explain_impact(analysis))
```

### Bridging code and data

Extractors give you each world; one edge connects them:

```python
from impactgraph import Edge, EdgeType

# "this API function reads the fact_booking table"
graph.add_edge(Edge(
    src="func:api/customers.py::customers_endpoint",
    dst="table:prod.analytics.fact_booking",
    type=EdgeType.DEPENDS_ON,
))
```

Now a change to a dbt model propagates all the way into your Python API — and vice versa.

## Use it from AI coding assistants

- **Claude Code skill:** copy `skills/impactgraph/` to `.claude/skills/impactgraph/` in your repo (or `~/.claude/skills/`). Then ask *"what breaks if I change dim_customer?"* or *"is this change safe to merge?"* — the skill runs `impactgraph diff` / `impact` and explains the JSON, marking inferred edges as heuristics.
- **MCP server:** `impactgraph mcp --graph impactgraph.json` exposes `impact`, `diff`, `find_nodes`, `paths`, `hotspots` over stdio. Register it in Claude Code / Cursor as an MCP server command.

## GitHub Action — impact comment on every PR

```yaml
- uses: <your-github-user>/impactgraph@main
  with:
    repo-path: src
    dbt-manifest: target/manifest.json
    fail-on: CRITICAL        # LOW | MEDIUM | HIGH | CRITICAL | NONE
```

See `examples/github-workflow-impact.yml` and `action.yml`.

## How impactgraph relates to Graphify and DataHub

People reasonably ask "isn't this Graphify / DataHub?" — it overlaps with both and is neither. The short version: **Graphify** helps an AI assistant *understand* a repo; **DataHub** is the company-wide *catalog* of data assets and lineage; **impactgraph** is the *pre-merge check* that asks "is this specific change safe?" across code **and** data.

| | Graphify | DataHub | impactgraph |
|---|---|---|---|
| Question answered | "Help my AI assistant understand this folder" | "What data exists, who owns it, how is it connected?" | "If I merge this change, what can break?" |
| Inputs | 13 languages (tree-sitter), docs, PDFs, images | 50+ connectors, runtime OpenLineage events | Python AST, dbt manifest, SQL, git diff, OpenLineage, DataHub lineage files, warehouse information_schema |
| Graph built by | AST + Claude (docs/images), edges tagged extracted/inferred | ingestion connectors | deterministic extractors only (no LLM in the graph); edges tagged extracted/inferred |
| Knows about application code | yes (structure / calls) | no | yes (files, functions, imports, calls) |
| Knows about data assets | SQL schemas as structure | yes — tables, columns, dashboards, quality, owners | dbt models, sources, tables, columns (with column-level lineage), exposures, owners |
| Direction-aware impact propagation | no | yes (data plane only) | yes, across code **and** data |
| Entry point | a folder | a table / dataset | a git diff (down to the changed function) |
| Output | queryable graph, HTML viz, wiki, report | UI, lineage/impact explorer, search, governance | blast radius tree, risk level, owners, test plan, JSON, interactive HTML, GraphML/DOT/Cypher |
| Infrastructure | none (local skill, MCP) | deployed platform (metadata service, DB, search, Kafka) | none (pip + a JSON file; runs in CI; skill + MCP) |
| AI role | extracts concepts from non-code inputs | n/a | explains the computed analysis only |

What impactgraph deliberately does **not** try to be: a catalog (search, glossary, governance policies, quality monitoring — use DataHub and import its lineage here), or a general code-understanding graph with docs/PDF/image ingestion (use Graphify).

Known limits today (honest): Python is the only code language parsed; code↔data bridge edges are still declared by hand (auto-detection is the top roadmap item); column lineage needs SQL (compiled dbt code, SQL files, view definitions or OpenLineage facets) — without it a column change falls back to a same-name heuristic that is marked *inferred*.

## How impact propagation works

Edges are semantic, and each type knows which way change flows:

| Edge | Direction of impact |
|---|---|
| `contains` (file → function, model → column) | forward — changing a file affects its members |
| `calls` (caller → callee) | reverse — changing the callee affects callers |
| `imports` (importer → imported) | reverse |
| `depends_on` (downstream → upstream; column → source column) | reverse — upstream change hits dependents |
| `writes_to` (model → table, job → dataset) | forward |
| `exposes` (model → dashboard) | forward |

`graph.impact(node)` runs a BFS honoring those directions and returns every affected node with its propagation depth. Every edge carries a provenance — `extracted` (read from an artifact) or `inferred` (name-based call resolution, same-name column heuristic) — and `--no-inferred` excludes the latter. Risk is scored deterministically (dashboards and APIs weigh more than functions; direct hits weigh more than distant ones).

## Node id conventions

```
file:models/customer.sql        func:src/api.py::customers_endpoint
class:src/models.py::Customer   dbt:dim_customer
source:raw.customers            exposure:revenue_report
table:prod.analytics.customer   column:dim_customer.customer_key   (column names lower-cased)
job:airflow/load_dim_customer
```

## Development

```bash
git clone <repo> && cd impactgraph
pip install -e .[dev]
pytest            # 70 tests, offline, ~4 s
```

## Roadmap

- Auto-detect code↔data bridge edges (SQL strings and warehouse-connector calls inside Python)
- More code languages via tree-sitter (TypeScript/JavaScript, Java/Scala first)
- Airflow DAG and AWS Lambda extractors; live DataHub GraphQL import
- Publish the skill to skill directories and the package to PyPI

## License

MIT
