import json
import sqlite3

import pytest

from datagraph import WarehouseExtractor
from datagraph.analysis.relationships import relationships
from datagraph.cli import main
from datagraph.extractors import connect_warehouse


@pytest.fixture
def sqlite_db(tmp_path):
    path = tmp_path / "shop.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT, country TEXT);
        CREATE TABLE products  (id INTEGER PRIMARY KEY, name TEXT, price REAL);
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id),
            product_id INTEGER,
            amount REAL,
            FOREIGN KEY (product_id) REFERENCES products(id)
        );
        CREATE VIEW v_order_emails AS
            SELECT o.id AS order_id, c.email, o.amount FROM orders o JOIN customers c ON c.id = o.customer_id;
        """
    )
    con.commit()
    con.close()
    return path


def test_sqlite_tables_columns_and_foreign_keys(sqlite_db):
    graph = WarehouseExtractor(str(sqlite_db)).extract()
    assert graph.get_node("table:orders") is not None
    assert graph.get_node("column:orders.customer_id").meta["data_type"] == "INTEGER"
    assert graph.get_node("column:customers.id").meta.get("primary_key") is True
    edges = {(e.src, e.dst, e.meta.get("via")) for e in graph.edges() if e.type.value == "depends_on"}
    assert ("column:orders.customer_id", "column:customers.id", "foreign_key") in edges
    assert ("column:orders.product_id", "column:products.id", "foreign_key") in edges
    assert ("table:orders", "table:customers", "foreign_key") in edges
    assert ("table:orders", "table:products", "foreign_key") in edges


def test_sqlite_view_lineage_and_impact(sqlite_db):
    pytest.importorskip("sqlglot")
    graph = WarehouseExtractor(str(sqlite_db)).extract()
    affected = graph.impact("column:customers.email")
    assert "column:v_order_emails.email" in affected          # view column derived from it
    affected_t = graph.impact("table:customers")
    assert "table:orders" in affected_t                      # FK child
    assert "table:v_order_emails" in affected_t              # view reads it


def test_connect_helper_and_dsn(sqlite_db):
    assert isinstance(connect_warehouse(f"sqlite:///{sqlite_db}"), sqlite3.Connection)
    assert isinstance(connect_warehouse(str(sqlite_db)), sqlite3.Connection)
    assert isinstance(connect_warehouse(":memory:"), sqlite3.Connection)


def test_relationships_summary(sqlite_db):
    graph = WarehouseExtractor(str(sqlite_db)).extract()
    rel = relationships(graph)
    ids = {t["id"] for t in rel["tables"]}
    assert {"table:customers", "table:orders", "table:products", "table:v_order_emails"} <= ids
    orders = next(t for t in rel["tables"] if t["id"] == "table:orders")
    assert {d["target"] for d in orders["depends_on"]} == {"table:customers", "table:products"}
    customers = next(t for t in rel["tables"] if t["id"] == "table:customers")
    assert any(d["source"] == "table:orders" for d in customers["dependents"])
    assert any(r["from"] == "column:orders.customer_id" and r["to"] == "column:customers.id" for r in rel["column_relationships"])
    filtered = relationships(graph, search="product")
    assert {t["id"] for t in filtered["tables"]} == {"table:products"}


def test_cli_build_warehouse_and_relationships(sqlite_db, tmp_path, capsys):
    gp = tmp_path / "g.json"
    assert main(["build", "--warehouse", str(sqlite_db), "-o", str(gp)]) == 0
    out = capsys.readouterr().out
    assert "warehouse:" in out
    assert main(["relationships", "--graph", str(gp), "--json"]) == 0
    rel = json.loads(capsys.readouterr().out)
    assert any(r["via"] == "foreign_key" for r in rel["table_relationships"])
    assert main(["relationships", "--graph", str(gp)]) == 0
    text = capsys.readouterr().out
    assert "table:orders" in text and "depends on table:customers" in text
    html = tmp_path / "schema.html"
    assert main(["html", "--all", "--with-columns", "--graph", str(gp), "-o", str(html)]) == 0
    assert "table:orders" in html.read_text(encoding="utf-8")
