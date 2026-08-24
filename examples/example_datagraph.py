"""
=====================================================================================
 datagraph - complete capability tour  (run me:  python example_datagraph.py)
=====================================================================================

What this script does
---------------------
It creates a small demo warehouse (SQLite file - no server needed), a small dbt
project, a small code repo, and then walks through EVERY capability of datagraph:

   1  Connect to a database and extract the schema        (WarehouseExtractor)
   2  Explore the graph                                   (nodes / edges / find / resolve)
   3  Lineage - table level and column level              (graph.lineage)
   4  Relationships - foreign keys and schema map         (relationships)
   5  Data profiling (+ automatic masking of PII columns) (profile_warehouse)
   6  Dimensional modelling - Kimball star schema         (star_schema / propose_from_table)
   7  Impact analysis - blast radius, risk, owners, tests (analyze_impact)
   8  Knowledge base for AI assistants                    (context / build_wiki)
   9  Visual output + exports                             (HTML, GraphML, DOT, Cypher, JSON)
  10  dbt project: models, owners, tests, column lineage  (DbtExtractor)
  11  Code + orchestration: Python, SQL, Airflow, Lambda, JS, OpenLineage, lineage files
  12  Merge everything into ONE graph, save/load, drift    (merge / link_table_aliases / diff_graphs)
  13  Plugins - add your own source                       (ExtractorPlugin / register)
  14  Optional AI layer (Anthropic / Bedrock-Nova / OpenAI-compatible)
  15  MCP tools - what a coding assistant sees            (build_tools)
  16  Maintenance + security helpers
  17  Other engines: DuckDB / PostgreSQL / MySQL / Snowflake
  18  CLI cheat sheet

Requirements
------------
    pip install "datagraph-core[sql]"        # sqlglot enables SQL / column lineage
    optional:  pip install duckdb            # section 17 second engine
    optional:  pip install "datagraph-core[ai]"       # real Claude explanations
    optional:  pip install "datagraph-core[bedrock]"  # Amazon Nova / Bedrock

Use YOUR database instead of the demo one
-----------------------------------------
    set DATAGRAPH_DEMO_DSN=postgresql+psycopg2://user:pw@localhost:5432/mydb
    set DATAGRAPH_DEMO_DSN=mysql+pymysql://user:pw@localhost:3306/mydb
    set DATAGRAPH_DEMO_DSN=snowflake://user:pw@account/db/schema?warehouse=wh
(then run this file again - section 17 will analyse it; use a READ-ONLY role)

Everything is written to ./datagraph_demo_out/ - open the .html files in a browser.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

# --------------------------------------------------------------------------- setup

OUT = Path(__file__).resolve().parent / "datagraph_demo_out"
OUT.mkdir(exist_ok=True)
DB = OUT / "warehouse.db"
USER_DSN = os.environ.get("DATAGRAPH_DEMO_DSN")  # optional: your own PostgreSQL / MySQL / Snowflake

try:
    import sqlglot  # noqa: F401
    HAS_SQLGLOT = True
except ImportError:                                   # pragma: no cover
    HAS_SQLGLOT = False


def banner(number: int, title: str) -> None:
    print("\n" + "=" * 86)
    print(f" {number}. {title}")
    print("=" * 86)


def show(label: str, value, limit: int = 12) -> None:
    """Print a short, readable view of a list / dict / string."""
    if isinstance(value, dict):
        items = list(value.items())[:limit]
        print(f"{label}: {len(value)} item(s)")
        for k, v in items:
            print(f"    {k}: {v}")
        if len(value) > limit:
            print(f"    ... {len(value) - limit} more")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)[:limit]
        print(f"{label}: {len(value)} item(s)")
        for v in items:
            print(f"    {v}")
        if len(value) > limit:
            print(f"    ... {len(value) - limit} more")
    else:
        print(f"{label}: {value}")


def build_demo_warehouse(path: Path) -> None:
    """A tiny but realistic warehouse: a star schema, a denormalised table and a view."""
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE dim_customer (
            customer_id INTEGER PRIMARY KEY,
            name        TEXT,
            email       TEXT,
            country     TEXT,
            segment     TEXT,
            updated_at  TEXT
        );
        CREATE TABLE dim_product (
            product_id   INTEGER PRIMARY KEY,
            product_name TEXT,
            category     TEXT,
            brand        TEXT,
            valid_from   TEXT,          -- SCD type 2 markers
            valid_to     TEXT,
            is_current   INTEGER
        );
        CREATE TABLE dim_date (
            date_key  INTEGER PRIMARY KEY,
            full_date TEXT,
            month     INTEGER,
            quarter   INTEGER,
            year      INTEGER
        );
        CREATE TABLE fact_sales (
            sale_id     INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES dim_customer(customer_id),
            product_id  INTEGER REFERENCES dim_product(product_id),
            date_key    INTEGER REFERENCES dim_date(date_key),
            quantity    INTEGER,
            amount      REAL,
            discount    REAL
        );
        -- a denormalised / "wide" table, the kind you inherit in brownfield projects
        CREATE TABLE wide_orders (
            order_id         INTEGER PRIMARY KEY,
            order_date       TEXT,
            customer_id      INTEGER,
            customer_name    TEXT,
            customer_country TEXT,
            product_sku      TEXT,
            product_category TEXT,
            status           TEXT,
            quantity         INTEGER,
            amount           REAL
        );
        CREATE VIEW v_sales_by_country AS
            SELECT c.country AS country, SUM(s.amount) AS amount
            FROM fact_sales s JOIN dim_customer c ON c.customer_id = s.customer_id
            GROUP BY c.country;
        """
    )
    con.executemany(
        "INSERT INTO dim_customer VALUES (?,?,?,?,?,?)",
        [(i, f"Customer {i}", f"user{i}@example.com" if i % 10 else None,
          ["IN", "US", "DE", "UK"][i % 4], ["SMB", "ENT"][i % 2], "2026-01-01") for i in range(1, 101)],
    )
    con.executemany(
        "INSERT INTO dim_product VALUES (?,?,?,?,?,?,?)",
        [(i, f"Product {i}", ["toys", "books", "tools"][i % 3], ["acme", "globex"][i % 2],
          "2026-01-01", None, 1) for i in range(1, 21)],
    )
    con.executemany(
        "INSERT INTO dim_date VALUES (?,?,?,?,?)",
        [(20260200 + d, f"2026-02-{d:02d}", 2, 1, 2026) for d in range(1, 29)],
    )
    con.executemany(
        "INSERT INTO fact_sales VALUES (?,?,?,?,?,?,?)",
        [(i, i % 100 + 1, i % 20 + 1, 20260200 + (i % 28) + 1, (i % 5) + 1, float(i), 0.0) for i in range(1, 2001)],
    )
    con.executemany(
        "INSERT INTO wide_orders VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(i, f"2026-03-{(i % 28) + 1:02d}", i % 50, f"Customer {i % 50}", ["IN", "US", "DE"][i % 3],
          f"SKU-{i % 20}", ["toys", "books"][i % 2], ["paid", "shipped", "returned"][i % 3],
          (i % 4) + 1, float(i)) for i in range(1, 501)],
    )
    con.commit()
    con.close()


print(__doc__.split("Requirements")[0])
print(f"output folder : {OUT}")
print(f"sqlglot       : {'installed' if HAS_SQLGLOT else 'NOT installed - SQL/column lineage will be limited'}")
build_demo_warehouse(DB)
print(f"demo warehouse: {DB}  (6 tables/views, 2600 rows)")


# =============================================================================== 1
banner(1, "Connect to a database and extract the schema")

from datagraph import WarehouseExtractor, NodeType, EdgeType          # noqa: E402
from datagraph.extractors.warehouse_extractor import connect          # noqa: E402

# Accepts: a file path, sqlite:///..., duckdb://..., any SQLAlchemy URL,
# or an already-open DB-API connection (so you can reuse your own pool).
warehouse = WarehouseExtractor(
    str(DB),
    # database="prod",            # catalog filter
    # schemas=["analytics"],      # only these schemas
    # dialect="snowflake",        # dialect used to parse view SQL
    view_lineage=True,            # parse CREATE VIEW ... into lineage edges
    foreign_keys=True,            # read declared FKs (table + column level)
)
graph = warehouse.extract()

counts = {}
for node in graph.nodes():
    counts[node.type.value] = counts.get(node.type.value, 0) + 1
show("nodes by type", counts)
print(f"edges: {len(graph.edges())}")
print("\nCLI equivalent: datagraph build --warehouse warehouse.db -o datagraph.json")


# =============================================================================== 2
banner(2, "Explore the graph")

print("tables and views:")
for node in sorted(graph.nodes(NodeType.TABLE) + graph.nodes(NodeType.VIEW), key=lambda n: n.id):
    print(f"    {node.id:<32} type={node.type.value:<6} meta={node.meta.get('table_type')}")

print("\nsearch (fuzzy):")
for node in graph.find("customer")[:6]:
    print(f"    {node.id}  ({node.type.value})")

print("\nresolve a short name -> full node id:")
resolved = graph.resolve("fact_sales")
print(f"    'fact_sales' -> {resolved.id}")

print("\ncolumns of fact_sales (with declared types and keys):")
for col in sorted([c for c in graph.nodes(NodeType.COLUMN) if c.meta.get("parent") == "table:fact_sales"],
                  key=lambda c: c.name):
    flags = " PK" if col.meta.get("primary_key") else ""
    print(f"    {col.name:<14} {col.meta.get('data_type')}{flags}")

print("\nedges around fact_sales (first 8):")
for edge in graph.edges_of("table:fact_sales")[:8]:
    print(f"    {edge.src} --{edge.type.value}--> {edge.dst}   [{edge.provenance}] {edge.meta.get('via', '')}")


# =============================================================================== 3
banner(3, "Lineage - where does it come from, what does it feed")

lin = graph.lineage("table:v_sales_by_country")
show("upstream of v_sales_by_country (what it reads)", {k: f"depth {v}" for k, v in lin["upstream"].items()})
show("downstream of dim_customer (what it feeds)", {k: f"depth {v}" for k, v in graph.lineage("table:dim_customer")["downstream"].items()})

print("\ncolumn-level lineage (from the view's SQL):")
for edge in graph.edges():
    src, dst = graph.get_node(edge.src), graph.get_node(edge.dst)
    if src and dst and src.type == NodeType.COLUMN and dst.type == NodeType.COLUMN:
        print(f"    {edge.src}  <-  {edge.dst}   [{edge.provenance}]")

print("\nupstream / downstream as trees (renderable):")
tree = graph.upstream_tree("table:v_sales_by_country", max_depth=2)
print(f"    root: {tree['name']} ({tree['type']}), children: {[c['name'] for c in tree['children']]}")

print("\nCLI: datagraph lineage v_sales_by_country --html lineage.html")


# =============================================================================== 4
banner(4, "Relationships - foreign keys and the schema map")

from datagraph.analysis.relationships import relationships                # noqa: E402

rel = relationships(graph, include_columns=True)
print("table relationships (foreign keys + lineage):")
for r in rel["table_relationships"]:
    print(f"    {r['source']}  --{r['via']}-->  {r['target']}   [{r['provenance']}]")

print("\ncolumn relationships:")
for r in rel["column_relationships"][:8]:
    print(f"    {r['from']}  ->  {r['to']}   [{r['via']}]")

one = next(t for t in rel["tables"] if t["id"] == "table:fact_sales")
print(f"\nfact_sales: {len(one['columns'])} columns, "
      f"depends on {[d['target'] for d in one['depends_on']]}, "
      f"fed to {[d['source'] for d in one['dependents']]}")

print("\nsearch just one area:  relationships(graph, search='customer')")
print("CLI: datagraph relationships --json")


# =============================================================================== 5
banner(5, "Data profiling (sensitive columns are masked automatically)")

from datagraph.profiling import profile_warehouse, profile_summary        # noqa: E402

profiles = profile_warehouse(
    str(DB), graph,
    # tables=["fact_sales", "dim_customer"],   # default: every warehouse table in the graph
    sample=100_000,        # rows sampled per table for column statistics
    top_values=True,       # collect the 5 most frequent values (skipped for sensitive columns)
    log=None,              # pass print to see progress
)
print("table profiles:")
for tid, prof in sorted(profiles.items()):
    print(f"    {tid:<32} rows={prof.get('row_count'):<6} freshness={prof.get('freshness')}")

print("\ncolumn profiles of dim_customer:")
for col in sorted([c for c in graph.nodes(NodeType.COLUMN) if c.meta.get("parent") == "table:dim_customer"],
                  key=lambda c: c.name):
    p = col.meta.get("profile") or {}
    masked = "  <- MASKED (looks like personal data)" if p.get("masked") else ""
    print(f"    {col.name:<12} nulls={p.get('null_pct')}%  distinct={p.get('distinct')}  "
          f"min={p.get('min')}  max={p.get('max')}  top={p.get('top_values')}{masked}")

print(f"\nprofile_summary(node) -> '{profile_summary(graph.get_node('table:fact_sales'))}'")
print("Profiles are stored on the graph, so they show up in relationships, context packs,")
print("the wiki, HTML tooltips - and they make the risk score data-aware.")
print("\nCLI: datagraph profile --warehouse warehouse.db [--no-top-values] [--sample 50000]")


# =============================================================================== 6
banner(6, "Dimensional modelling - Kimball star schema, deterministically")

from datagraph.analysis.modeling import (                                  # noqa: E402
    classify_tables, star_schema, propose_from_table, to_markdown, to_mermaid, fk_links,
)

cls = classify_tables(graph)
print("table classification (role, confidence, why):")
for tid, c in sorted(cls.items()):
    print(f"    {tid:<32} {c['role']:<10} {c['confidence']}   {'; '.join(c['reasons'][:2])}")

print("\ncolumn roles in fact_sales:", cls["table:fact_sales"]["columns"])

model = star_schema(graph, include_inferred=True)   # include_inferred=False -> declared FKs only
print(f"\nstandard: {model['standard']}")
print(f"facts: {[f['id'] for f in model['facts']]}")
print(f"dimensions: {[d['id'] for d in model['dimensions']]}")
print(f"conformed dimensions: {model['conformed_dimensions']}")
show("bus matrix (fact -> dimensions)", model["bus_matrix"])

fact = next(f for f in model["facts"] if f["id"] == "table:fact_sales")
print("\nfact_sales, the Kimball four steps:")
for step, value in fact["kimball"].items():
    print(f"    {step}: {value}")

print("\nslowly changing dimensions:")
for tid, scd in model["scd"].items():
    print(f"    {tid:<24} type {scd['scd_type']}   {scd['recommendation']}")

show("issues / recommendations", model["issues"])

(OUT / "MODEL.md").write_text(to_markdown(model, "Demo dimensional model"), encoding="utf-8")
(OUT / "er-diagram.mmd").write_text(to_mermaid(model), encoding="utf-8")
print(f"\nwritten: {OUT / 'MODEL.md'} (report) and {OUT / 'er-diagram.mmd'} (Mermaid ER diagram)")

print("\n--- proposing a star schema from a wide / denormalised table ---")
proposal = propose_from_table(graph, "wide_orders")
print(f"proposed fact: {proposal['fact']['name']}")
print(f"    grain    : {proposal['fact']['grain']}")
print(f"    measures : {proposal['fact']['measures']}")
print(f"    degenerate dimensions: {proposal['fact']['degenerate_dimensions']}")
for dim in proposal["dimensions"]:
    print(f"    dimension {dim['name']:<16} from columns {dim['source_columns']}")
show("notes", proposal["notes"])

print("\nkey links found (declared vs inferred from names):")
for link in fk_links(graph)[:10]:
    print(f"    {link['from_table']}.{link['from_column']} -> {link['to_table']}.{link['to_column']}  [{link['provenance']}]")

print("\nCLI: datagraph model --markdown MODEL.md --mermaid er.mmd --from-table wide_orders")


# =============================================================================== 7
banner(7, "Impact analysis - what breaks if I change this")

from datagraph import analyze_impact                                       # noqa: E402
from datagraph.report import render_analysis                               # noqa: E402

analysis = analyze_impact(graph, ["table:dim_customer"], max_depth=None, include_inferred=True)
print(f"risk       : {analysis.risk['level']} (score {analysis.risk['score']})")
print(f"affected   : {len(analysis.affected)} node(s) -> {analysis.summary_by_type()}")
print(f"owners     : {analysis.owners or 'none in this graph (owners come from dbt / DataHub metadata)'}")
show("recommended tests", analysis.recommended_tests)

print("\nchanging a single COLUMN (the classic 'can I rename this?' question):")
col_analysis = analyze_impact(graph, ["column:dim_customer.country"])
show("affected by column:dim_customer.country", col_analysis.affected)

print("\nevery propagation path from dim_customer to the view:")
for path in graph.impact_paths("table:dim_customer", "table:v_sales_by_country"):
    print("    " + "  ->  ".join(path))

print("\nhotspots (where a change hurts most):")
for row in graph.hotspots(top=5):
    print(f"    {row['id']:<32} blast_radius={row['blast_radius']:<4} in={row['in_degree']} out={row['out_degree']}")

print("\nartifact-backed edges only (drops heuristics):")
print(f"    with heuristics : {len(analyze_impact(graph, ['table:dim_customer']).affected)} affected")
print(f"    --no-inferred   : {len(analyze_impact(graph, ['table:dim_customer'], include_inferred=False).affected)} affected")

print("\nfull terminal report:")
render_analysis(graph, analysis)

print("\nJSON for machines: analysis.to_dict() ->", sorted(analysis.to_dict().keys()))
print("CLI: datagraph impact dim_customer --json")


# =============================================================================== 8
banner(8, "Knowledge base for AI assistants")

from datagraph.knowledge import context, build_wiki                        # noqa: E402

pack = context(graph, "fact_sales", depth=2)
print("--- context pack for fact_sales (paste this into any AI assistant) ---")
print(pack[:1600] + ("\n... (truncated)" if len(pack) > 1600 else ""))

stats = build_wiki(graph, OUT / "wiki", title="Demo warehouse knowledge base")
print(f"\nwiki: {stats['pages']} pages for {stats['nodes']} nodes -> {OUT / 'wiki'}")
show("wiki files", sorted(p.name for p in (OUT / "wiki").iterdir()))
print("    index.md         - every asset, grouped by type")
print("    GRAPH_REPORT.md  - hotspots, models without tests, ownerless nodes, roots, leaves")
print("    MODEL.md         - the dimensional model")
print("    llms.txt         - entry point for RAG tools")
print("    nodes/*.md       - one cross-linked page per table / model / function")
print("\nCLI: datagraph context fact_sales   |   datagraph wiki -o kb/")


# =============================================================================== 9
banner(9, "Visual output and exports")

from datagraph.html_report import render_html, render_lineage_html, render_graph_html   # noqa: E402

(OUT / "impact.html").write_text(render_html(graph, analysis, title="Impact of changing dim_customer"), encoding="utf-8")
(OUT / "lineage.html").write_text(render_lineage_html(graph, "table:fact_sales", title="Lineage of fact_sales"), encoding="utf-8")
(OUT / "graph.html").write_text(render_graph_html(graph, hide_columns=True, title="Demo warehouse"), encoding="utf-8")
graph.to_graphml(OUT / "graph.graphml")          # for Gephi / yEd / networkx
(OUT / "graph.dot").write_text(graph.to_dot(), encoding="utf-8")
(OUT / "graph.cypher").write_text(graph.to_cypher(), encoding="utf-8")
graph.save(OUT / "datagraph.json")
print("written (open the .html files in a browser):")
for name in ["impact.html", "lineage.html", "graph.html", "graph.graphml", "graph.dot", "graph.cypher", "datagraph.json"]:
    print(f"    {OUT / name}")
print("\nCLI: datagraph html --all -o graph.html   |   datagraph export --format cypher -o g.cypher")


# =============================================================================== 10
banner(10, "dbt project - models, owners, tests, column-level lineage")

from datagraph import DbtExtractor                                          # noqa: E402

dbt_dir = OUT / "dbt_project"
dbt_dir.mkdir(exist_ok=True)
manifest = {
    "metadata": {"project_name": "demo", "adapter_type": "snowflake"},
    "nodes": {
        "model.demo.stg_customers": {
            "resource_type": "model", "name": "stg_customers", "original_file_path": "models/stg_customers.sql",
            "database": "prod", "schema": "staging", "config": {"materialized": "view"},
            "description": "Cleaned customer rows from the source system.",
            "depends_on": {"nodes": ["source.demo.raw.customers"]},
            "columns": {"customer_id": {"data_type": "NUMBER"}, "country": {"data_type": "VARCHAR"}},
            "compiled_code": "select customer_id, upper(country) as country from raw.customers",
            "meta": {"owner": "data-platform"},
        },
        "model.demo.dim_customer": {
            "resource_type": "model", "name": "dim_customer", "original_file_path": "models/dim_customer.sql",
            "database": "prod", "schema": "analytics", "config": {"materialized": "table"},
            "description": "Customer dimension (one row per customer).",
            "depends_on": {"nodes": ["model.demo.stg_customers"]},
            "columns": {"customer_key": {"data_type": "NUMBER"}, "country": {"data_type": "VARCHAR"}},
            "compiled_code": "select customer_id as customer_key, country from staging.stg_customers",
            "meta": {"owner": "data-platform"},
        },
        "model.demo.fact_booking": {
            "resource_type": "model", "name": "fact_booking", "original_file_path": "models/fact_booking.sql",
            "database": "prod", "schema": "analytics", "config": {"materialized": "table"},
            "description": "Bookings fact.",
            "depends_on": {"nodes": ["model.demo.dim_customer"]},
            "columns": {"customer_key": {"data_type": "NUMBER"}, "amount": {"data_type": "NUMBER"}},
            "compiled_code": "select customer_key, sum(amount) as amount from analytics.dim_customer group by 1",
            "meta": {"owner": "finance"},
        },
        "test.demo.unique_dim_customer_key": {
            "resource_type": "test", "name": "unique_dim_customer_customer_key",
            "depends_on": {"nodes": ["model.demo.dim_customer"]},
        },
        "test.demo.not_null_dim_customer_key": {
            "resource_type": "test", "name": "not_null_dim_customer_customer_key",
            "depends_on": {"nodes": ["model.demo.dim_customer"]},
        },
    },
    "sources": {
        "source.demo.raw.customers": {
            "resource_type": "source", "name": "customers", "source_name": "raw",
            "database": "prod", "schema": "raw", "identifier": "customers",
            "columns": {"customer_id": {"data_type": "NUMBER"}, "country": {"data_type": "VARCHAR"}},
            "meta": {"owner": "ingestion"},
        }
    },
    "exposures": {
        "exposure.demo.revenue_report": {
            "name": "revenue_report", "type": "dashboard",
            "owner": {"name": "finance", "email": "finance@example.com"},
            "depends_on": {"nodes": ["model.demo.fact_booking"]},
        }
    },
}
(dbt_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
# catalog.json (from `dbt docs generate`) gives the real columns, so SELECT * can be expanded
catalog = {"nodes": {"model.demo.dim_customer": {"columns": {"customer_key": {"type": "NUMBER"}, "country": {"type": "VARCHAR"}}}},
           "sources": {}}
(dbt_dir / "catalog.json").write_text(json.dumps(catalog, indent=2), encoding="utf-8")

dbt_graph = DbtExtractor(dbt_dir / "manifest.json", catalog_path=dbt_dir / "catalog.json").extract()
print(f"dbt graph: {len(dbt_graph)} nodes, {len(dbt_graph.edges())} edges")
for node in sorted(dbt_graph.nodes(), key=lambda n: n.id):
    if node.type != NodeType.COLUMN:
        extra = f" owner={node.owner}" if node.owner else ""
        print(f"    {node.id:<38} {node.type.value}{extra}")

model_node = dbt_graph.get_node("dbt:dim_customer")
print(f"\ndim_customer.meta['tests'] = {model_node.meta.get('tests')}")
print(f"dim_customer.meta['sql']   = {model_node.meta.get('sql')}")

print("\ncolumn-level lineage parsed from compiled SQL:")
for edge in dbt_graph.edges():
    s, d = dbt_graph.get_node(edge.src), dbt_graph.get_node(edge.dst)
    if s and d and s.type == NodeType.COLUMN and d.type == NodeType.COLUMN:
        print(f"    {edge.src}  <-  {edge.dst}")

dbt_analysis = analyze_impact(dbt_graph, ["dbt:dim_customer"])
print(f"\nchanging dim_customer: risk {dbt_analysis.risk['level']}, "
      f"{len(dbt_analysis.affected)} affected, notify {dbt_analysis.owners}")
show("recommended tests", dbt_analysis.recommended_tests)
print("\nCLI: datagraph build --dbt-manifest target/manifest.json --dbt-catalog target/catalog.json")


# =============================================================================== 11
banner(11, "Code and orchestration - Python, SQL, Airflow, Lambda, JS, OpenLineage")

from datagraph import (                                                      # noqa: E402
    PythonExtractor, SqlExtractor, AirflowExtractor, LambdaExtractor, JsExtractor,
    OpenLineageExtractor, LineageFileExtractor,
)

project = OUT / "demo_project"
(project / "etl").mkdir(parents=True, exist_ok=True)
(project / "sql").mkdir(exist_ok=True)
(project / "dags").mkdir(exist_ok=True)
(project / "web").mkdir(exist_ok=True)

# --- Python (functions, calls, imports) + SQL strings inside code (the code<->data bridge)
(project / "etl" / "__init__.py").write_text("", encoding="utf-8")
(project / "etl" / "load_customers.py").write_text(
    '''"""Demo ETL."""


def load_customers(conn):
    """Reads the customer dimension and writes the sales fact."""
    rows = conn.execute("SELECT customer_id, name, country FROM dim_customer").fetchall()
    conn.execute("INSERT INTO fact_sales (customer_id, amount) VALUES (?, ?)", rows)
    return rows


def refresh_marts(conn):
    """Calls load_customers - so it is affected when load_customers changes."""
    return load_customers(conn)
''', encoding="utf-8")

# --- a raw SQL file
(project / "sql" / "customer_summary.sql").write_text(
    "CREATE VIEW customer_summary AS\n"
    "SELECT c.country AS country, COUNT(*) AS customers, SUM(s.amount) AS revenue\n"
    "FROM dim_customer c JOIN fact_sales s ON s.customer_id = c.customer_id\n"
    "GROUP BY c.country;\n", encoding="utf-8")

# --- an Airflow DAG
(project / "dags" / "nightly_sales.py").write_text(
    '''from airflow import DAG
from airflow.operators.python import PythonOperator
from etl.load_customers import load_customers, refresh_marts

with DAG("nightly_sales", schedule_interval="@daily") as dag:
    extract = PythonOperator(task_id="extract_customers", python_callable=load_customers)
    build = PythonOperator(task_id="build_marts", python_callable=refresh_marts)
    extract >> build
''', encoding="utf-8")

# --- an AWS Lambda / serverless template
(project / "serverless.yml").write_text(
    '''service: sales-api
provider:
  name: aws
  runtime: python3.12
  environment:
    SALES_TABLE: fact_sales
functions:
  getSales:
    handler: etl/load_customers.load_customers
    events:
      - http:
          path: /sales
          method: get
''', encoding="utf-8")

# --- a JS/TS front end
(project / "web" / "dashboard.js").write_text(
    '''import { format } from "./format.js";

export async function loadSales() {
  const res = await fetch("/sales");
  return format(await res.json());
}
''', encoding="utf-8")

# --- OpenLineage events (what Airflow / Spark / Marquez emit at run time)
ol_events = [
    {"eventType": "COMPLETE",
     "job": {"namespace": "airflow", "name": "nightly_sales.build_marts"},
     "inputs": [{"namespace": "sqlite", "name": "dim_customer",
                 "facets": {"schema": {"fields": [{"name": "customer_id", "type": "INTEGER"}]}}}],
     "outputs": [{"namespace": "sqlite", "name": "fact_sales",
                  "facets": {"columnLineage": {"fields": {"customer_id": {"inputFields": [
                      {"namespace": "sqlite", "name": "dim_customer", "field": "customer_id"}]}}}}}]},
]
(OUT / "openlineage.ndjson").write_text("\n".join(json.dumps(e) for e in ol_events), encoding="utf-8")

# --- a curated lineage file (DataHub "lineage file" format; JSON or YAML)
(OUT / "lineage.json").write_text(json.dumps({
    "version": 1,
    "lineage": [{
        "entity": {"name": "reporting.sales_summary", "type": "dataset", "platform": "snowflake"},
        "upstream": [{"entity": {"name": "fact_sales"}}],
        "owner": "analytics-team",
    }],
}, indent=2), encoding="utf-8")

fragments = {
    "python  ": PythonExtractor(project).extract(),   # scan the whole project so ids match Airflow/Lambda
    "sql     ": SqlExtractor(project / "sql").extract() if HAS_SQLGLOT else None,
    "airflow ": AirflowExtractor(project / "dags").extract(),
    "lambda  ": LambdaExtractor(project / "serverless.yml", code_root=project).extract(),
    "js      ": JsExtractor(project / "web").extract(),
    "openlin.": OpenLineageExtractor(OUT / "openlineage.ndjson").extract(),
    "lineage ": LineageFileExtractor(OUT / "lineage.json").extract(),
}
for name, frag in fragments.items():
    if frag is None:
        print(f"    {name}: skipped (needs sqlglot)")
        continue
    kinds = {}
    for n in frag.nodes():
        kinds[n.type.value] = kinds.get(n.type.value, 0) + 1
    print(f"    {name}: {len(frag)} nodes {kinds}, {len(frag.edges())} edges")

py = fragments["python  "]
print("\nthe automatic code <-> data bridge (SQL found inside Python):")
for edge in py.edges():
    if edge.type in (EdgeType.DEPENDS_ON, EdgeType.WRITES_TO):
        print(f"    {edge.src}  --{edge.type.value}-->  {edge.dst}   [{edge.provenance}]")

print("\nAirflow tasks and their dependencies:")
for edge in fragments["airflow "].edges():
    print(f"    {edge.src}  --{edge.type.value}-->  {edge.dst}")


# =============================================================================== 12
banner(12, "Merge everything into ONE graph, save, reload, detect drift")

from datagraph import ImpactGraph, diff_graphs                               # noqa: E402

full = ImpactGraph()
full.merge(graph)          # the warehouse
full.merge(dbt_graph)      # the dbt project
for frag in fragments.values():
    if frag is not None:
        full.merge(frag)
linked = full.link_table_aliases()   # analytics.orders  ==  prod.analytics.orders
print(f"merged graph: {len(full)} nodes, {len(full.edges())} edges ({linked} alias link(s) added)")

kinds = {}
for n in full.nodes():
    kinds[n.type.value] = kinds.get(n.type.value, 0) + 1
show("nodes by type", kinds)

print("\nnow one question spans code AND data:")
code_impact = analyze_impact(full, ["etl/load_customers.py::load_customers"])
print(f"    changing load_customers() -> risk {code_impact.risk['level']}, {len(code_impact.affected)} affected")
show("    affected", list(code_impact.affected)[:10])

full.save(OUT / "full-graph.json")
reloaded = ImpactGraph.load(OUT / "full-graph.json")
print(f"\nsaved and reloaded: {len(reloaded)} nodes (same graph, portable JSON)")

# drift: what changed between two snapshots of the graph?
changed_warehouse = ImpactGraph()
changed_warehouse.merge(graph)
changed_warehouse.add_node(type(graph.get_node("table:fact_sales"))(
    id="table:fact_refunds", type=NodeType.TABLE, name="fact_refunds"))
drift = diff_graphs(graph, changed_warehouse)
show("graph drift (added / removed nodes and edges)", {k: v for k, v in drift.items() if v})
print("CLI: datagraph graph-diff old.json new.json")


# =============================================================================== 13
banner(13, "Plugins - teach datagraph about your own tool")

from datagraph import ExtractorPlugin, register                              # noqa: E402
from datagraph.extractors.registry import plugins                            # noqa: E402
from datagraph import Node, Edge                                             # noqa: E402


class LookerLikeExtractor:
    """Any class with .extract() -> ImpactGraph can be plugged in."""

    def __init__(self, url, token=None):
        self.url, self.token = url, token

    def extract(self) -> ImpactGraph:
        g = ImpactGraph()
        g.add_node(Node(id="exposure:exec_dashboard", type=NodeType.DASHBOARD,
                        name="exec_dashboard", meta={"owner": "exec-team", "url": self.url}))
        g.add_edge(Edge(src="table:fact_sales", dst="exposure:exec_dashboard", type=EdgeType.EXPOSES))
        return g


register(ExtractorPlugin(name="lookerlike", factory=LookerLikeExtractor,
                         help="Demo BI extractor", options={"token": "API token"}))
print("registered plugins:", [(p.name, p.help) for p in plugins()])

full.merge(LookerLikeExtractor("https://bi.example.com").extract())
print("\nnow the dashboard is part of the blast radius:")
bi = analyze_impact(full, ["table:fact_sales"])
print(f"    changing fact_sales -> {sorted(k for k in bi.affected if k.startswith('exposure:'))}, "
      f"notify {bi.owners}")
print("\nA packaged plugin declares an entry point instead:")
print('    [project.entry-points."datagraph.extractors"]')
print('    lookerlike = "my_package:LookerLikeExtractor"')
print("...and it automatically becomes:  datagraph build --lookerlike https://bi.example.com")


# =============================================================================== 14
banner(14, "Optional AI layer - Anthropic / Amazon Bedrock (Nova) / OpenAI-compatible")

from datagraph.ai import explain_impact, suggest_lineage, apply_suggestions   # noqa: E402
from datagraph.ai.providers import LLMProvider                                # noqa: E402


class DemoProvider(LLMProvider):
    """A stub so this example runs offline. Real usage: provider='bedrock' etc."""

    name, model = "demo", "demo-1"

    def complete(self, system, user, *, max_tokens=4096, json_schema=None):
        if json_schema:   # a lineage-suggestion request
            return json.dumps({"relationships": [{
                "kind": "table", "source": "table:wide_orders", "target": "table:dim_customer",
                "confidence": 0.82, "reason": "wide_orders.customer_id matches dim_customer.customer_id",
            }]})
        return ("Changing dim_customer affects the sales fact and the country view; "
                "validate the country values and re-run the dbt tests before deploying.")


print("1) explain a result in plain language (the LLM never builds the graph):")
print("   " + explain_impact(analysis, provider=DemoProvider()))

print("\n2) ask for relationships the parsers could not derive (a labelled FALLBACK):")
suggestions = suggest_lineage(graph, provider=DemoProvider())
for s in suggestions:
    print(f"   {s['source']} -> {s['target']}  confidence={s['confidence']}  ({s['reason']})")
added = apply_suggestions(graph, suggestions, min_confidence=0.6)
print(f"   applied {added} suggestion(s); they are tagged provenance='llm' and dropped by --no-inferred")
for e in graph.edges():
    if e.provenance == "llm":
        print(f"   -> {e.src} --{e.type.value}--> {e.dst}  [{e.provenance}] {e.meta.get('reason')}")

print("\nReal providers (credentials always come from the environment, never from code):")
print("   Anthropic : explain_impact(analysis)                                   # ANTHROPIC_API_KEY")
print("   Bedrock   : explain_impact(analysis, provider='bedrock',")
print("                              model='amazon.nova-pro-v1:0')               # AWS profile / role")
print("   OpenAI-*  : provider='openai'  + DATAGRAPH_LLM_BASE_URL (Ollama, vLLM, Azure, Groq ...)")
print("   or set DATAGRAPH_LLM_PROVIDER / DATAGRAPH_LLM_MODEL once and forget it")
print("CLI: datagraph explain fact_sales --provider bedrock --model amazon.nova-lite-v1:0")


# =============================================================================== 15
banner(15, "MCP - the tools an AI coding assistant gets")

from datagraph.mcp_server import build_tools                                  # noqa: E402

full.save(OUT / "datagraph.json")
tools = build_tools(str(OUT / "datagraph.json"))
print("tools exposed over MCP:", sorted(tools))
print("\ntools['lineage']('fact_sales') ->")
lineage_result = tools["lineage"]("fact_sales")
print(f"    upstream={list(lineage_result['upstream'])[:4]} downstream={list(lineage_result['downstream'])[:4]}")
print("\ntools['model']() -> facts:", [f["id"] for f in tools["model"]()["facts"]])
print("tools['hotspots']()[0] ->", tools["hotspots"]()[0])
print("\ntools['context']('dim_customer') (first lines):")
print("    " + "\n    ".join(tools["context"]("dim_customer").splitlines()[:6]))
print("\nRun the server:  datagraph mcp --graph datagraph.json      (stdio, read-only)")
print("Claude Code:     claude mcp add datagraph -- python -m datagraph.cli mcp --graph datagraph.json")


# =============================================================================== 16
banner(16, "Maintenance and security helpers")

from datagraph import maintenance                                             # noqa: E402
from datagraph.security import redact_dsn, is_sensitive_column                # noqa: E402

inputs = [str(project / "etl"), str(dbt_dir / "manifest.json")]
print("fingerprint of the inputs:", maintenance.fingerprint(inputs)[:16], "...")
maintenance.write_cache(str(OUT / "datagraph.json"), inputs)
print("is the graph up to date?", maintenance.is_up_to_date(str(OUT / "datagraph.json"), inputs))
print("   -> this is what `datagraph build --update` uses to skip needless rebuilds")
print("   -> `datagraph watch ...` rebuilds on change; `datagraph hook-install` adds a git hook")

print("\nsecrets are never stored or logged:")
print("   ", redact_dsn("snowflake://alice:S3cr3t@acct/db?warehouse=wh"))
print("   ", redact_dsn("postgresql://user:pw@host:5432/db"))
print("\ncolumns that are auto-masked while profiling:")
for name in ["email", "customer_name", "phone_number", "card_number", "api_token", "customer_id", "amount"]:
    print(f"    {name:<16} sensitive={is_sensitive_column(name)}")


# =============================================================================== 17
banner(17, "Other engines - DuckDB now, PostgreSQL / MySQL / Snowflake the same way")

try:
    import duckdb

    duck_path = OUT / "warehouse.duckdb"
    if duck_path.exists():
        duck_path.unlink()
    dcon = duckdb.connect(str(duck_path))
    dcon.execute("CREATE TABLE dim_customer (customer_id INTEGER PRIMARY KEY, country VARCHAR, email VARCHAR)")
    dcon.execute("CREATE TABLE fact_sales (sale_id INTEGER PRIMARY KEY, customer_id INTEGER, amount DOUBLE)")
    dcon.execute("CREATE VIEW v_country AS SELECT c.country, SUM(s.amount) amount "
                 "FROM fact_sales s JOIN dim_customer c ON c.customer_id = s.customer_id GROUP BY 1")
    dcon.execute("INSERT INTO dim_customer SELECT i, 'IN', 'u' || i || '@x.com' FROM range(1, 51) t(i)")
    dcon.execute("INSERT INTO fact_sales SELECT i, (i % 50) + 1, i * 1.0 FROM range(1, 201) t(i)")

    duck_graph = WarehouseExtractor(dcon, dialect="duckdb").extract()   # reads information_schema
    duck_graph.link_table_aliases()   # v_country's SQL names tables without the catalog prefix
    print(f"DuckDB (information_schema path): {len(duck_graph)} nodes, {len(duck_graph.edges())} edges")
    show("   tables/views", [n.id for n in duck_graph.nodes(NodeType.TABLE) + duck_graph.nodes(NodeType.VIEW)])
    profile_warehouse(dcon, duck_graph, sample=1000)
    prof = duck_graph.get_node("table:memory.main.fact_sales") or duck_graph.get_node("table:main.fact_sales")
    if prof:
        print("   profiled fact_sales:", prof.meta.get("profile", {}).get("row_count"), "rows")
    dcon.close()
except ImportError:
    print("duckdb is not installed - `pip install duckdb` to see this section run")

print("""
Connection strings for the engines datagraph is designed for
    PostgreSQL : postgresql+psycopg2://user:pw@host:5432/db     (pip install psycopg2-binary)
    MySQL      : mysql+pymysql://user:pw@host:3306/db           (pip install pymysql)
    SQL Server : mssql+pyodbc://user:pw@dsn                     (pip install pyodbc)
    Snowflake  : snowflake://user:pw@account/db/schema?warehouse=wh   (pip install snowflake-sqlalchemy)
    BigQuery   : bigquery://project/dataset                     (pip install sqlalchemy-bigquery)
    Redshift   : redshift+psycopg2://user:pw@host:5439/db       (pip install sqlalchemy-redshift)
    DuckDB     : duckdb:///path/to.duckdb          SQLite: path/to.db  or  sqlite:///path/to.db
Always use a READ-ONLY role. The password is never written to the graph, cache or logs.""")

if USER_DSN:
    print(f"\nDATAGRAPH_DEMO_DSN is set -> analysing {redact_dsn(USER_DSN)}")
    try:
        user_conn = connect(USER_DSN)
        user_graph = WarehouseExtractor(user_conn).extract()
        print(f"    {len(user_graph)} nodes, {len(user_graph.edges())} edges")
        user_model = star_schema(user_graph)
        print(f"    facts: {[f['id'] for f in user_model['facts']][:5]}")
        print(f"    dimensions: {[d['id'] for d in user_model['dimensions']][:5]}")
        show("    issues", user_model["issues"][:5])
        build_wiki(user_graph, OUT / "your_wiki", title="Your warehouse")
        print(f"    wiki -> {OUT / 'your_wiki'}")
    except Exception as exc:                                    # noqa: BLE001
        print(f"    could not connect: {exc}")
else:
    print("\n(set DATAGRAPH_DEMO_DSN to run the whole flow against your own database)")


# =============================================================================== 18
banner(18, "The same things from the command line")

print("""
  # one shot: connection in -> lineage, profiling, model, wiki out
  datagraph analyze --warehouse "postgresql+psycopg2://user:pw@host/db" --schemas analytics -o out/

  # build a graph from anything you have
  datagraph build --warehouse warehouse.db --dbt-manifest target/manifest.json \\
                  --dbt-catalog target/catalog.json --sql ./sql --repo ./src \\
                  --airflow ./dags --lambda serverless.yml --js ./web \\
                  --openlineage events.ndjson -o datagraph.json --update

  # ask questions
  datagraph lineage fact_sales --html lineage.html
  datagraph relationships --search customer --json
  datagraph profile --warehouse warehouse.db
  datagraph model --markdown MODEL.md --mermaid er.mmd
  datagraph model --from-table wide_orders
  datagraph impact dim_customer --json
  datagraph diff --repo . --base origin/main
  datagraph paths dim_customer v_sales_by_country
  datagraph hotspots --top 10
  datagraph context fact_sales
  datagraph wiki -o kb/
  datagraph html --all -o graph.html
  datagraph export --format graphml -o graph.graphml
  datagraph graph-diff old.json new.json
  datagraph explain fact_sales --provider bedrock --model amazon.nova-pro-v1:0
  datagraph enrich --dry-run
  datagraph mcp --graph datagraph.json
  datagraph plugins

  (on Windows, if the 'datagraph' launcher is blocked:  python -m datagraph.cli ...)
""")

print("=" * 86)
print(f" Done. Open the results in: {OUT}")
print("   impact.html / lineage.html / graph.html   - interactive views")
print("   MODEL.md / er-diagram.mmd                 - the dimensional model")
print("   wiki/                                     - knowledge base for AI assistants")
print("   datagraph.json                            - the graph itself")
print("=" * 86)
