"""Live-engine integration tests: PostgreSQL and MySQL.

Skipped unless a DSN is provided, so the normal offline suite is unaffected:

    set DATAGRAPH_TEST_POSTGRES=postgresql+psycopg2://postgres:postgres@localhost:5432/postgres
    set DATAGRAPH_TEST_MYSQL=mysql+pymysql://root:mysql@localhost:3306/datagraph_test
    pytest -m integration

CI runs both against service containers, which is what turns "should work on any
information_schema engine" into something actually verified.
"""

import os

import pytest

from datagraph import WarehouseExtractor, analyze_impact
from datagraph.analysis.modeling import star_schema
from datagraph.analysis.relationships import relationships
from datagraph.extractors.warehouse_extractor import connect
from datagraph.profiling import profile_warehouse
from datagraph.usage import usage_stats

pytestmark = pytest.mark.integration

POSTGRES = os.environ.get("DATAGRAPH_TEST_POSTGRES")
MYSQL = os.environ.get("DATAGRAPH_TEST_MYSQL")

DDL = [
    "DROP VIEW IF EXISTS v_sales_by_country",
    "DROP TABLE IF EXISTS fact_sales",
    "DROP TABLE IF EXISTS dim_customer",
    """CREATE TABLE dim_customer (
           customer_id INTEGER PRIMARY KEY,
           email       VARCHAR(120),
           country     VARCHAR(2)
       )""",
    """CREATE TABLE fact_sales (
           sale_id     INTEGER PRIMARY KEY,
           customer_id INTEGER REFERENCES dim_customer(customer_id),
           amount      DECIMAL(10,2)
       )""",
    """CREATE VIEW v_sales_by_country AS
           SELECT c.country AS country, SUM(s.amount) AS amount
           FROM fact_sales s JOIN dim_customer c ON c.customer_id = s.customer_id
           GROUP BY c.country""",
]


def _seed(connection, mysql: bool = False):
    cursor = connection.cursor()
    for statement in DDL:
        if mysql and "REFERENCES" in statement:
            statement = statement.replace(
                "customer_id INTEGER REFERENCES dim_customer(customer_id)",
                "customer_id INTEGER, FOREIGN KEY (customer_id) REFERENCES dim_customer(customer_id)",
            )
        cursor.execute(statement)
    placeholder = "%s"
    cursor.executemany(
        f"INSERT INTO dim_customer VALUES ({placeholder}, {placeholder}, {placeholder})",
        [(i, f"user{i}@example.com", ["IN", "US", "DE"][i % 3]) for i in range(1, 21)],
    )
    cursor.executemany(
        f"INSERT INTO fact_sales VALUES ({placeholder}, {placeholder}, {placeholder})",
        [(i, (i % 20) + 1, float(i)) for i in range(1, 101)],
    )
    connection.commit()
    cursor.close()


def _check(connection, dialect: str, schema: str):
    graph = WarehouseExtractor(connection, schemas=[schema], dialect=dialect).extract()

    ids = {n.id for n in graph.nodes()}
    assert any(i.endswith("dim_customer") for i in ids), ids
    assert any(i.endswith("fact_sales") for i in ids)
    assert any(i.endswith("v_sales_by_country") for i in ids)
    # no engine-owned objects leaked in (the 0.8.4 fix)
    assert not [i for i in ids if "pg_catalog" in i or "performance_schema" in i or "information_schema" in i]

    columns = {n.name for n in graph.nodes() if n.type.value == "column"}
    assert {"customer_id", "email", "country", "amount"} <= columns

    # declared foreign key -> table and column edges
    fk_edges = [e for e in graph.edges() if e.meta.get("via") == "foreign_key"]
    assert fk_edges, "no foreign keys read from information_schema"

    rel = relationships(graph)
    assert any(r["via"] == "foreign_key" for r in rel["table_relationships"])

    # profiling against the real engine, with masking
    profiles = profile_warehouse(connection, graph, sample=1000)
    customer = next(v for k, v in profiles.items() if k.endswith("dim_customer"))
    sales = next(v for k, v in profiles.items() if k.endswith("fact_sales"))
    assert customer["row_count"] == 20 and sales["row_count"] == 100
    email = next(n for n in graph.nodes() if n.name == "email" and n.type.value == "column")
    assert email.meta["profile"]["masked"] is True and email.meta["profile"]["min"] is None
    country = next(n for n in graph.nodes() if n.name == "country" and n.type.value == "column")
    assert country.meta["profile"]["distinct"] == 3

    # impact + modelling work on real metadata
    customer_id = next(i for i in ids if i.endswith("dim_customer"))
    analysis = analyze_impact(graph, [customer_id])
    assert analysis.affected and analysis.risk["level"] in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    model = star_schema(graph)
    assert any(f["id"].endswith("fact_sales") for f in model["facts"])

    # usage: the engine's own counters (must not raise, may legitimately be zero)
    stats = usage_stats(connection, graph, dialect=dialect, log=None)
    assert isinstance(stats, dict)


@pytest.mark.skipif(not POSTGRES, reason="set DATAGRAPH_TEST_POSTGRES to run")
def test_postgres_end_to_end():
    connection = connect(POSTGRES)
    try:
        _seed(connection)
        _check(connection, "postgres", "public")
    finally:
        connection.close()


@pytest.mark.skipif(not MYSQL, reason="set DATAGRAPH_TEST_MYSQL to run")
def test_mysql_end_to_end():
    connection = connect(MYSQL)
    try:
        _seed(connection, mysql=True)
        _check(connection, "mysql", os.environ.get("DATAGRAPH_TEST_MYSQL_DB", "datagraph_test"))
    finally:
        connection.close()
