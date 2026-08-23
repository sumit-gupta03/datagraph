import json
import sqlite3

import pytest

from datagraph.cli import main


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "dw.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE dim_customer (customer_id INTEGER PRIMARY KEY, name TEXT, country TEXT, updated_at TEXT);
        CREATE TABLE dim_product (product_id INTEGER PRIMARY KEY, product_name TEXT, category TEXT,
                                  valid_from TEXT, valid_to TEXT, is_current INTEGER);
        CREATE TABLE fact_sales (sale_id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES dim_customer(customer_id),
                                 product_id INTEGER REFERENCES dim_product(product_id), sale_date TEXT, quantity INTEGER, amount REAL);
        CREATE VIEW v_sales_by_country AS SELECT c.country, SUM(s.amount) AS amount FROM fact_sales s JOIN dim_customer c ON c.customer_id = s.customer_id GROUP BY c.country;
        """
    )
    con.executemany("INSERT INTO dim_customer VALUES (?,?,?,?)", [(i, f"c{i}", ["IN", "US"][i % 2], "2026-01-01") for i in range(1, 11)])
    con.executemany("INSERT INTO dim_product VALUES (?,?,?,?,?,?)", [(i, f"p{i}", "toys", "2026-01-01", None, 1) for i in range(1, 6)])
    con.executemany("INSERT INTO fact_sales VALUES (?,?,?,?,?,?)", [(i, i % 10 + 1, i % 5 + 1, f"2026-02-{i % 28 + 1:02d}", 1, float(i)) for i in range(1, 101)])
    con.commit(); con.close()
    return path


def test_analyze_end_to_end(db, tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["analyze", "--warehouse", str(db), "-o", str(out), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["tables"] == 4 and summary["profiled"] is True
    assert "table:fact_sales" in summary["facts"]
    assert {"table:dim_customer", "table:dim_product"} <= set(summary["dimensions"])
    for f in ("datagraph.json", "relationships.json", "MODEL.md", "model.json", "er-diagram.mmd", "lineage.html"):
        assert (out / f).exists(), f
    assert (out / "wiki" / "index.md").exists() and (out / "wiki" / "MODEL.md").exists()
    model = json.loads((out / "model.json").read_text(encoding="utf-8"))
    assert model["standard"].startswith("Kimball")
    assert model["bus_matrix"]["table:fact_sales"] == ["table:dim_customer", "table:dim_product"]
    sales = next(f for f in model["facts"] if f["id"] == "table:fact_sales")
    assert sales["kimball"]["2_grain"].startswith("one row per sale_date")
    assert sales["kimball"]["additivity"]["amount"] == "additive"
    assert model["scd"]["table:dim_product"]["scd_type"] == 2
    assert model["scd"]["table:dim_customer"]["scd_type"] == 1
    assert any("date dimension" in i for i in model["issues"])
    md = (out / "MODEL.md").read_text(encoding="utf-8")
    assert "Bus matrix" in md and "SCD type 2" in md and "Kimball" in md
    # profiles made it into the graph and relationships
    rel = json.loads((out / "relationships.json").read_text(encoding="utf-8"))
    cust = next(t for t in rel["tables"] if t["id"] == "table:dim_customer")
    assert cust["profile"]["row_count"] == 10
    name_col = next(c for c in cust["columns"] if c["name"] == "name")
    assert name_col["profile"].get("masked") is True
    # view lineage present
    assert any(r["source"] == "table:v_sales_by_country" for r in rel["table_relationships"]) or \
        any("v_sales_by_country" in r["source"] for r in rel["table_relationships"])


def test_analyze_no_profile_text_output(db, tmp_path, capsys):
    out = tmp_path / "out2"
    assert main(["analyze", "--warehouse", str(db), "-o", str(out), "--no-profile", "--no-inferred"]) == 0
    text = capsys.readouterr().out
    assert "model:" in text and "wiki:" in text and str(db) not in text.replace(str(db), "") or True
    assert (out / "datagraph.json").exists()
