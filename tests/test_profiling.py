import json
import sqlite3

import pytest

from datagraph import WarehouseExtractor, analyze_impact
from datagraph.ai.lineage import schema_summary
from datagraph.cli import main
from datagraph.profiling import profile_summary, profile_warehouse


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "p.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT, country TEXT, created_at TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), amount REAL, order_date TEXT);
        CREATE TABLE empty_t (id INTEGER);
        """
    )
    con.executemany("INSERT INTO customers VALUES (?,?,?,?)",
                    [(i, f"u{i}@x.com" if i % 10 else None, ["IN", "US", "DE"][i % 3], f"2026-01-{(i % 28) + 1:02d}") for i in range(1, 101)])
    con.executemany("INSERT INTO orders VALUES (?,?,?,?)",
                    [(i, (i % 100) + 1, float(i), f"2026-02-{(i % 28) + 1:02d}") for i in range(1, 501)])
    con.commit(); con.close()
    return path


def test_profile_tables_and_columns(db):
    graph = WarehouseExtractor(str(db)).extract()
    results = profile_warehouse(str(db), graph, sample=1000)
    assert results["table:customers"]["row_count"] == 100
    assert results["table:orders"]["row_count"] == 500
    assert results["table:empty_t"]["row_count"] == 0
    email = graph.get_node("column:customers.email").meta["profile"]
    assert email["null_pct"] == 10.0
    assert email["distinct"] == 90
    country = graph.get_node("column:customers.country").meta["profile"]
    assert country["distinct"] == 3 and len(country["top_values"]) == 3
    amount = graph.get_node("column:orders.amount").meta["profile"]
    assert amount["min"] == 1.0 and amount["max"] == 500.0
    assert results["table:orders"]["freshness"].startswith("2026-02-28")
    assert "rows" in profile_summary(graph.get_node("table:orders"))
    assert "10.0% null" in profile_summary(graph.get_node("column:customers.email"))


def test_profile_feeds_risk_and_llm_payload(db):
    graph = WarehouseExtractor(str(db)).extract()
    profile_warehouse(str(db), graph, sample=1000, top_values=False)
    s = schema_summary(graph)
    cust = next(t for t in s["tables"] if t["id"] == "table:customers")
    assert cust["row_count"] == 100
    assert any("profile" in c and c["profile"].get("distinct") == 90 for c in cust["columns"])
    # empty table counts half: impact of customers reaches orders (FK); risk computed without error
    a = analyze_impact(graph, ["table:customers"])
    assert a.risk["score"] > 0


def test_profile_cli_and_relationships_show_profile(db, tmp_path, capsys):
    gp = tmp_path / "g.json"
    assert main(["build", "--warehouse", str(db), "-o", str(gp)]) == 0
    capsys.readouterr()
    assert main(["profile", "--warehouse", str(db), "--graph", str(gp), "--tables", "customers,orders", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["table:customers"]["row_count"] == 100 and "table:empty_t" not in res
    assert main(["relationships", "--graph", str(gp), "--json"]) == 0
    rel = json.loads(capsys.readouterr().out)
    cust = next(t for t in rel["tables"] if t["id"] == "table:customers")
    assert cust["profile"]["row_count"] == 100
    assert any(c.get("profile", {}).get("null_pct") == 10.0 for c in cust["columns"])
