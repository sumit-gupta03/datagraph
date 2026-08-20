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
| Python source | `ast` | files, functions, classes, imports, call edges |
| dbt project | `manifest.json` | models, sources, seeds, exposures, the resolved DAG, materialized tables, columns |
| Raw SQL | `sqlglot` | table/view lineage and output columns |
| Git | `git diff` | which files *and which functions* actually changed |

Graph algorithms compute the blast radius, a deterministic risk score, and a test plan. The AI layer (optional) only **explains** the already-computed analysis — so you get LLM readability with graph-level trust.

## Install

```bash
pip install impactgraph            # core (Python + dbt + git extractors)
pip install impactgraph[sql]       # + SQL lineage via sqlglot
pip install impactgraph[ai]        # + AI explanations via the Claude API
pip install impactgraph[all]       # everything
```

## Quick start (CLI)

```bash
# 1. Build the unified graph from your artifacts
impactgraph build --repo ./src --dbt-manifest target/manifest.json --sql ./sql -o impactgraph.json

# 2. What breaks if I change this dbt model?
impactgraph impact dbt:customer

# 3. What breaks given my current (uncommitted) git diff?
impactgraph diff --repo . --graph impactgraph.json

# 4. Search the graph
impactgraph nodes --search customer --type dbt_model

# 5. AI explanation of the analysis (requires [ai] + ANTHROPIC_API_KEY)
impactgraph explain dbt:customer
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

Recommended tests:
  ✓ dbt build --select customer+ dim_customer+ fact_booking+
  ✓ Run a schema/contract check on prod.analytics.fact_booking
  ✓ Manually validate 'revenue_report' after deploy (numbers & filters)
```

## Quick start (Python API)

```python
from impactgraph import ImpactGraph, PythonExtractor, DbtExtractor, analyze_impact

graph = ImpactGraph()
graph.merge(PythonExtractor("./src").extract())
graph.merge(DbtExtractor("target/manifest.json").extract())

analysis = analyze_impact(graph, ["dbt:customer"])
print(analysis.risk)               # {'score': 24.5, 'level': 'HIGH'}
print(analysis.affected)           # {node_id: depth, ...}
print(analysis.recommended_tests)  # ['dbt build --select ...', ...]

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

## How impact propagation works

Edges are semantic, and each type knows which way change flows:

| Edge | Direction of impact |
|---|---|
| `contains` (file → function) | forward — changing a file affects its members |
| `calls` (caller → callee) | reverse — changing the callee affects callers |
| `imports` (importer → imported) | reverse |
| `depends_on` (downstream → upstream) | reverse — upstream change hits dependents |
| `writes_to` (model → table) | forward |
| `exposes` (model → dashboard) | forward |

`graph.impact(node)` runs a BFS honoring those directions and returns every affected node with its propagation depth. Risk is scored deterministically (dashboards and APIs weigh more than functions; direct hits weigh more than distant ones).

## Node id conventions

```
file:models/customer.sql        func:src/api.py::customers_endpoint
class:src/models.py::Customer   dbt:dim_customer
source:raw.customers            exposure:revenue_report
table:prod.analytics.customer   column:dim_customer.customer_id
```

## Development

```bash
git clone <repo> && cd impactgraph
pip install -e .[dev]
pytest
```

## Roadmap

- Column-level lineage through dbt compiled SQL
- Airflow DAG / AWS Lambda extractors
- OpenLineage import/export
- GitHub Action: post the impact report on every PR

## License

MIT
