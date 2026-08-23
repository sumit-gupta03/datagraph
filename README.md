# impactgraph

**AI-powered Change Impact Graph for data and code systems.**

Answer the two questions every data / platform engineer asks:

> *"If I change this file, function, dbt model, SQL column, or table — what can break?"*
> *"Where does this table / column come from, and what does it feed?"*

Projects like Graphify / Code-Graph-RAG build graphs from source code; DataHub and OpenLineage focus on data lineage. `impactgraph` connects both worlds into **one unified graph** and answers from it in seconds, locally:

```
Git Change → Python/JS Function → Lambda → API → Table → dbt Model → Report / Dashboard
                     Airflow task ──┘            ▲
           warehouse foreign keys / views ───────┘
```

<p align="center">
  <img src="docs/images/lineage-jaffle-customers.png" alt="Lineage of the customers model in dbt's jaffle_shop project" width="900"><br>
  <em>impactgraph lineage customers --html — upstream (left) through staging models to seed files, downstream (right) to the table and its columns. Real output on dbt's public jaffle_shop project.</em>
</p>

## The key idea

**The graph is never built by an LLM.** It is constructed deterministically from real engineering artifacts; graph algorithms compute blast radius, lineage, risk, owners and a test plan; an LLM may only *explain* the result — or, as a clearly-labelled **fallback**, *suggest* relationships the parsers could not derive (tagged `llm`, excludable).

| Artifact | Flag | What it contributes |
|---|---|---|
| Python source | `--repo` | files, functions, classes, imports, calls (*inferred*), and **SQL found inside the code → table edges** (the automatic code↔data bridge) |
| JavaScript / TypeScript | `--js` | files, functions, imports, calls, SQL-in-code |
| dbt project | `--dbt-manifest` (+ `--dbt-catalog`) | models, sources, seeds, exposures, the DAG, materialized tables, columns, **owners**, and **column-to-column lineage** from `compiled_code` (expands `select *` using `catalog.json`) |
| Raw SQL | `--sql` | table/view lineage **and column lineage** (aliases, CTEs, renames) via sqlglot |
| Warehouse / database | `--warehouse DSN` | real tables, columns + types, **foreign keys** (table↔table and column↔column), view lineage — from `information_schema`, or a **SQLite** file for zero-setup |
| Git | `diff` | which files *and which functions* changed right now |
| Airflow DAGs | `--airflow` | DAGs, tasks, task dependencies (`>>`, lists, `chain`), `python_callable` links, SQL in operators → tables |
| AWS Lambda | `--lambda` | serverless.yml / SAM / CloudFormation: lambdas → handler functions, HTTP APIs, S3/SQS/DynamoDB event sources and env-referenced tables |
| OpenLineage events | `--openlineage` | datasets, jobs, schema + `columnLineage` facets, ownership (what Airflow / Marquez / DataHub already observed) |
| DataHub | `--lineage-file`, `--datahub URL` | curated lineage files, or a **live GraphQL import** of datasets, owners, upstream and fine-grained (column) lineage |

Table names at different qualification (`analytics.fact_booking` in code vs `prod.analytics.fact_booking` in dbt) are linked automatically.

## Install

```bash
pip install impactgraph            # core (all extractors; sqlglot needed for SQL/column lineage)
pip install impactgraph[sql]       # + sqlglot
pip install impactgraph[ai]        # + Claude explanations and LLM lineage fallback
pip install impactgraph[mcp]       # + MCP server for Claude Code / Cursor / Codex
pip install impactgraph[all]       # everything (also PyYAML for YAML lineage files / serverless.yml)
```

## Quick start

```bash
# 1. Build the graph from whatever you have (any combination)
impactgraph build --repo ./src --dbt-manifest target/manifest.json --dbt-catalog target/catalog.json \
                  --sql ./sql --airflow ./dags --lambda template.yaml --js ./web \
                  --openlineage events.ndjson --warehouse "snowflake://..." -o impactgraph.json

# 2. Impact — what breaks?
impactgraph impact dbt:customer                         # a model / table / column / function / task
impactgraph diff --repo . --graph impactgraph.json      # my current uncommitted change  ← the CI command

# 3. Lineage & relationships — where does it come from, what does it feed, how are tables related?
impactgraph lineage table:prod.analytics.dim_customer   # upstream + downstream trees (--html for a picture)
impactgraph relationships                               # every table: columns, foreign keys, lineage (--json)
impactgraph paths dbt:customer exposure:revenue_report  # every propagation path
impactgraph hotspots                                    # where a change hurts most

# 4. Pictures
impactgraph html dbt:customer -o impact.html            # interactive blast radius
impactgraph lineage customers --html lineage.html       # interactive lineage
impactgraph html --all -o graph.html                    # the whole graph (--with-columns for columns)
impactgraph export --format graphml -o g.graphml        # also dot | cypher | json

# 5. Keep it fresh / extend
impactgraph build ... --update                          # skip when inputs unchanged
impactgraph watch ... ;  impactgraph hook-install --git-repo . ...
impactgraph graph-diff old.json new.json                # schema / dependency drift
impactgraph enrich --dry-run                            # LLM suggestions for SQL the parsers could not read (needs [ai])

# 6. AI
impactgraph explain dbt:customer                        # plain-language explanation (needs [ai])
impactgraph mcp --graph impactgraph.json                # MCP server for coding assistants (needs [mcp])
```

<p align="center">
  <img src="docs/images/impact-demo.png" alt="Interactive blast-radius view" width="900"><br>
  <em>impactgraph html models/customer.sql — change in one SQL file → models → tables → dashboards and the Python API, with risk, owners to notify and the test plan.</em>
</p>

Terminal output of `impactgraph impact`:

```
⚠ Change Impact                     Changed: customer      Risk: HIGH (score 24.5)

⬢ customer (dbt_model)
├── ⬢ dim_customer (dbt_model) via depends_on
│   └── ⬢ fact_booking (dbt_model) via depends_on
│       ├── 📊 revenue_report (dashboard) via exposes
│       └── 📊 customer_dashboard (dashboard) via exposes
└── ▤ prod.analytics.customer (view) via writes_to

Affected: 3 dbt model(s) · 2 dashboard(s) · 2 table(s)
Notify (owners of affected artifacts):  finance: revenue_report · growth: customer_dashboard
Recommended tests:
  ✓ dbt build --select customer+ dim_customer+ fact_booking+
  ✓ Run a schema/contract check on prod.analytics.fact_booking
  ✓ Manually validate 'revenue_report' after deploy (numbers & filters)
```

`--json` for machines · `--no-inferred` to keep only artifact-backed edges (drops name-resolved calls, same-name column guesses and `llm` suggestions) · `--html out.html` for the picture.

## Python API

```python
from impactgraph import (ImpactGraph, PythonExtractor, DbtExtractor, WarehouseExtractor,
                         AirflowExtractor, LambdaExtractor, JsExtractor, OpenLineageExtractor,
                         LineageFileExtractor, DataHubExtractor, analyze_impact)

graph = ImpactGraph()
graph.merge(PythonExtractor("./src").extract())
graph.merge(DbtExtractor("target/manifest.json", catalog_path="target/catalog.json").extract())
graph.merge(WarehouseExtractor("warehouse.db").extract())        # or any DB-API connection / SQLAlchemy URL
graph.merge(AirflowExtractor("./dags").extract())
graph.link_table_aliases()

analysis = analyze_impact(graph, ["dbt:customer"])
print(analysis.risk, analysis.owners, analysis.recommended_tests)

print(graph.lineage("table:prod.analytics.dim_customer"))       # {'upstream': {...}, 'downstream': {...}}
from impactgraph.analysis.relationships import relationships
print(relationships(graph)["table_relationships"])              # foreign keys + lineage between tables

# Optional AI (pip install impactgraph[ai])
from impactgraph.ai import explain_impact, suggest_lineage, apply_suggestions
print(explain_impact(analysis))
apply_suggestions(graph, suggest_lineage(graph), min_confidence=0.7)   # tagged provenance=llm
```

## Use it from AI coding assistants

- **Claude Code skill:** copy `skills/impactgraph/` to `.claude/skills/impactgraph/` (or `~/.claude/skills/`). Ask *"what breaks if I change dim_customer?"*, *"where does fact_booking come from?"*, *"how are these tables related?"*.
- **MCP server:** `impactgraph mcp --graph impactgraph.json` exposes `impact`, `diff`, `find_nodes`, `paths`, `hotspots`, `lineage`, `relationships`.

## GitHub Action — impact comment on every PR

```yaml
- uses: sumit-gupta03/impactgraph@main
  with:
    repo-path: src
    dbt-manifest: target/manifest.json
    fail-on: CRITICAL        # LOW | MEDIUM | HIGH | CRITICAL | NONE
```

See `examples/github-workflow-impact.yml` and `action.yml`. Tagging `vX.Y.Z` builds and creates a GitHub Release (`.github/workflows/publish.yml`); PyPI publishing switches on once the trusted publisher is configured (see the workflow header).

## How it compares: Graphify · DataHub · OpenLineage · impactgraph

| | Graphify | DataHub | OpenLineage | impactgraph |
|---|---|---|---|---|
| What it is | A skill that turns a folder into a knowledge graph for AI assistants | A deployed metadata platform / catalog | An open **standard + spec** for emitting lineage events (plus Marquez as a reference server) | A pip library + CLI + skill for pre-merge impact, lineage and schema relationships |
| Question answered | "Help my AI assistant understand this repo" | "What data exists, who owns it, how is it connected, is it healthy?" | "What did this job read and write (at run time)?" | "If I change this, what breaks? Where does it come from? How are tables related?" |
| Inputs | 13 languages via tree-sitter, docs, PDFs, images | 50+ connectors, OpenLineage events | Emitters in Airflow, Spark, dbt, Flink… | Python/JS AST, dbt manifest+catalog, SQL, warehouse information_schema (FKs), git diff, Airflow, Lambda, OpenLineage, DataHub |
| Graph built by | AST + Claude for non-code | ingestion connectors | the emitting jobs | deterministic extractors; optional `llm` fallback clearly tagged |
| Knows application code | yes (structure) | no | no | yes — functions, calls, SQL-in-code, Lambda handlers, Airflow callables |
| Column-level lineage | no | yes (connectors) | yes (facet) | yes (sqlglot, catalog-aware; imports OL/DataHub column lineage) |
| Foreign-key / schema relationships | no | yes | no | yes (`relationships`, FK edges, schema drift) |
| Direction-aware impact + risk + test plan | no | impact view only | no | yes, across code and data |
| Entry point | a folder | a dataset | a job run | a git diff (to the changed function), or any node |
| Display | HTML graph, wiki | web UI | via a backend (Marquez/DataHub) | interactive HTML (impact / lineage / whole graph), terminal trees, GraphML/DOT/Cypher |
| Infrastructure | none | platform (DB, search, Kafka) | events need a backend | none — pip, a JSON file, runs in CI; skill + MCP |
| AI role | extracts concepts from docs/images | n/a | n/a | explains results; optional fallback suggestions tagged `llm` |

**Positioning:** OpenLineage is the *wire format* lineage travels in; DataHub is the *catalog* it lands in; Graphify is the *repo map* for an assistant; impactgraph is the *pre-merge check and lineage/relationship explorer* that also reads your code — and it **imports** OpenLineage events and DataHub lineage rather than competing with them. What it deliberately is not: a catalog (search, glossary, governance, quality monitoring).

**Known limits:** code languages are Python and JS/TS (regex-based for JS); call edges are name-resolved (tagged *inferred*); column lineage needs SQL or a catalog — otherwise a same-name heuristic (tagged *inferred*) or the opt-in `llm` fallback applies.

## How impact propagation works

Edges are semantic and each type knows which way change flows: `contains`, `writes_to`, `exposes` forward; `calls`, `imports`, `depends_on` reverse. `graph.impact(node)` is a BFS honouring those directions; `graph.upstream(node)` is the inverse; `graph.lineage(node)` is both. Every edge carries a provenance — `extracted`, `inferred` or `llm`.

## Node id conventions

```
file:models/customer.sql       func:src/api.py::customers_endpoint    class:src/models.py::Customer
dbt:dim_customer               source:raw.customers                   exposure:revenue_report
table:prod.analytics.customer  column:dim_customer.customer_key       job:airflow/load_dim_customer
dag:nightly_bookings           task:nightly_bookings/build_dim        lambda:GetBookings    api:GET /bookings
```

## Development

```bash
git clone https://github.com/sumit-gupta03/impactgraph && cd impactgraph
pip install -e .[dev]
pytest            # 108 tests, offline, ~7 s — includes dbt's real jaffle_shop project as a fixture
```

## Roadmap

- Tree-sitter based parsers for Java/Scala/Go (today: Python via ast, JS/TS via regex)
- Airflow TaskFlow-decorated task bodies, Dagster/Prefect extractors
- Incremental per-file rebuilds (today `--update` skips unchanged inputs)
- PyPI release (workflow ready; needs the trusted publisher enabled)

## License

MIT
