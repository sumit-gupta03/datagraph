import json
import sqlite3
from pathlib import Path

import pytest

from datagraph import DbtExtractor, WarehouseExtractor, classify_tables, propose_from_table, star_schema
from datagraph.analysis.modeling import fk_links, to_markdown, to_mermaid
from datagraph.graph import Edge, EdgeType, ImpactGraph, Node, NodeType
from datagraph.cli import main
from datagraph.knowledge import build_wiki, context
from datagraph.mcp_server import build_tools
from datagraph.profiling import profile_warehouse

MANIFEST = Path(__file__).resolve().parents[1] / "examples" / "jaffle_shop" / "manifest.json"


@pytest.fixture
def star_db(tmp_path):
    path = tmp_path / "star.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE dim_customer (customer_id INTEGER PRIMARY KEY, name TEXT, email TEXT, country TEXT, segment TEXT);
        CREATE TABLE dim_product (product_id INTEGER PRIMARY KEY, product_name TEXT, category TEXT, brand TEXT);
        CREATE TABLE dim_store (store_id INTEGER PRIMARY KEY, store_name TEXT, city TEXT, region_id INTEGER REFERENCES dim_region(region_id));
        CREATE TABLE dim_region (region_id INTEGER PRIMARY KEY, region_name TEXT);
        CREATE TABLE fact_sales (sale_id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES dim_customer(customer_id),
                                 product_id INTEGER REFERENCES dim_product(product_id), store_id INTEGER REFERENCES dim_store(store_id),
                                 sale_date TEXT, quantity INTEGER, amount REAL, discount REAL);
        CREATE TABLE fact_returns (return_id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES dim_customer(customer_id),
                                   product_id INTEGER REFERENCES dim_product(product_id), return_date TEXT, refund_amount REAL);
        CREATE TABLE bridge_product_tag (product_id INTEGER REFERENCES dim_product(product_id), tag_id INTEGER REFERENCES dim_tag(tag_id));
        CREATE TABLE dim_tag (tag_id INTEGER PRIMARY KEY, tag TEXT);
        CREATE TABLE wide_orders (order_id INTEGER PRIMARY KEY, order_date TEXT, customer_id INTEGER, customer_name TEXT,
                                  customer_country TEXT, product_sku TEXT, product_category TEXT, status TEXT, quantity INTEGER, amount REAL);
        """
    )
    con.executemany("INSERT INTO wide_orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(i, f"2026-03-{(i % 28) + 1:02d}", i % 50, f"cust{i % 50}", ["IN", "US", "DE"][i % 3], f"SKU-{i % 20}",
                      ["toys", "books"][i % 2], ["paid", "shipped", "returned"][i % 3], i % 5 + 1, float(i)) for i in range(1, 301)])
    con.commit(); con.close()
    return path


def test_classify_star_from_foreign_keys(star_db):
    g = WarehouseExtractor(str(star_db)).extract()
    cls = classify_tables(g)
    assert cls["table:fact_sales"]["role"] == "fact"
    assert cls["table:fact_returns"]["role"] == "fact"
    assert cls["table:dim_customer"]["role"] == "dimension"
    assert cls["table:dim_product"]["role"] == "dimension"
    assert cls["table:bridge_product_tag"]["role"] == "bridge"
    assert cls["table:fact_sales"]["columns"]["amount"] == "measure"
    assert cls["table:fact_sales"]["columns"]["sale_date"] == "date"
    assert cls["table:fact_sales"]["columns"]["customer_id"] == "fk"
    assert cls["table:dim_customer"]["columns"]["customer_id"] == "pk"
    model = star_schema(g)
    sales = next(f for f in model["facts"] if f["id"] == "table:fact_sales")
    assert {d["table"] for d in sales["dimensions"]} == {"table:dim_customer", "table:dim_product", "table:dim_store"}
    assert all(d["provenance"] == "extracted" for d in sales["dimensions"])
    assert "sale_date" in sales["grain"] and sales["measures"] == ["amount", "discount", "quantity"]
    assert set(model["conformed_dimensions"]) >= {"table:dim_customer", "table:dim_product"}
    assert any("snowflaked" in i for i in model["issues"])  # dim_store -> dim_region
    mm = to_mermaid(model)
    assert "erDiagram" in mm and "fact_sales }o--|| dim_customer" in mm
    md = to_markdown(model)
    assert "## Facts" in md and "**conformed**" in md and "```mermaid" in md


def test_inferred_links_when_no_foreign_keys():
    g = DbtExtractor(MANIFEST).extract()  # jaffle_shop: orders.customer_id -> customers by name only
    links = fk_links(g)
    assert any(l["from_table"] == "dbt:orders" and l["to_table"] == "dbt:customers" and l["provenance"] == "inferred" for l in links)
    assert not [l for l in fk_links(g, include_inferred=False)]
    cls = classify_tables(g)
    assert cls["dbt:orders"]["role"] == "fact"
    assert cls["dbt:customers"]["role"] == "dimension"
    model = star_schema(g)
    orders = next(f for f in model["facts"] if f["id"] == "dbt:orders")
    assert any(d["table"] == "dbt:customers" and d["provenance"] == "inferred" for d in orders["dimensions"])
    assert any("inferred from names" in i for i in model["issues"])


def test_propose_star_from_wide_table(star_db):
    g = WarehouseExtractor(str(star_db)).extract()
    profile_warehouse(str(star_db), g, tables=["table:wide_orders"], sample=1000)
    prop = propose_from_table(g, "wide_orders")
    names = {d["name"] for d in prop["dimensions"]}
    assert {"dim_customer", "dim_product", "dim_date"} <= names
    assert "dim_status" in names
    cust = next(d for d in prop["dimensions"] if d["name"] == "dim_customer")
    assert set(cust["source_columns"]) == {"customer_id", "customer_name", "customer_country"}
    assert prop["fact"]["name"] == "fact_wide_orders"
    assert set(prop["fact"]["measures"]) == {"quantity", "amount"}
    assert "order_date" in prop["fact"]["grain"]
    assert "erDiagram" in to_mermaid(prop) and "fact_wide_orders }o--|| dim_date" in to_mermaid(prop)
    assert "Proposed star schema" in to_markdown(prop)


def test_model_cli_wiki_context_mcp(star_db, tmp_path, capsys):
    gp = tmp_path / "g.json"
    assert main(["build", "--warehouse", str(star_db), "-o", str(gp)]) == 0
    capsys.readouterr()
    assert main(["model", "--graph", str(gp), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(f["id"] == "table:fact_sales" for f in payload["facts"])
    mmd = tmp_path / "er.mmd"
    assert main(["model", "--graph", str(gp), "--mermaid", str(mmd), "--markdown", str(tmp_path / "m.md")]) == 0
    assert "erDiagram" in mmd.read_text(encoding="utf-8")
    assert main(["model", "--graph", str(gp), "--from-table", "wide_orders"]) == 0
    assert "dim_customer" in capsys.readouterr().out
    assert main(["model", "--graph", str(gp), "--from-table", "nope"]) == 2
    # wiki has MODEL.md; context shows the role; MCP exposes model
    g = WarehouseExtractor(str(star_db)).extract()
    build_wiki(g, tmp_path / "kb")
    assert "fact_sales" in (tmp_path / "kb" / "MODEL.md").read_text(encoding="utf-8")
    assert "modelling role: fact" in context(g, "fact_sales")
    tools = build_tools(str(gp))
    m = tools["model"]()
    assert "mermaid" in m and any(f["id"] == "table:fact_sales" for f in m["facts"])
    p = tools["model"](from_table="wide_orders")
    assert p["fact"]["name"] == "fact_wide_orders"


def test_alias_nodes_are_not_reported_as_unclassifiable():
    """A view's short name for a table is not a second table to complain about.

    `link_table_aliases` adds `table:fact_sales` beside `table:prod.public.fact_sales` so impact
    crosses both spellings. Classifying the alias produced "2 table(s) could not be classified"
    against tables the user never wrote — noise at the top of every model report.
    """
    from datagraph.analysis.modeling import classify_tables, star_schema

    graph = ImpactGraph()
    graph.add_node(Node(id="table:prod.public.dim_customer", type=NodeType.TABLE,
                        name="prod.public.dim_customer", meta={"source": "warehouse"}))
    graph.add_node(Node(id="column:prod.public.dim_customer.customer_id", type=NodeType.COLUMN,
                        name="customer_id", meta={"parent": "table:prod.public.dim_customer",
                                                  "data_type": "integer", "primary_key": True}))
    graph.add_edge(Edge(src="table:prod.public.dim_customer",
                        dst="column:prod.public.dim_customer.customer_id", type=EdgeType.CONTAINS))
    graph.add_node(Node(id="table:dim_customer", type=NodeType.TABLE, name="dim_customer"))
    assert graph.link_table_aliases() == 1

    classified = classify_tables(graph)
    assert "table:dim_customer" not in classified
    assert "table:prod.public.dim_customer" in classified
    assert not [i for i in star_schema(graph)["issues"] if "could not be classified" in i]
