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


def test_system_catalogs_and_schemas_are_excluded(tmp_path):
    """MySQL's mysql/sys/performance_schema and DuckDB's system/temp catalogs are engine-owned."""
    from datagraph.extractors.warehouse_extractor import WarehouseExtractor as WE

    db = tmp_path / "w.db"
    con = sqlite3.connect(db); con.execute("CREATE TABLE t (id INTEGER)"); con.commit(); con.close()
    where = WE(str(db))._where()
    for schema in ("information_schema", "pg_catalog", "mysql", "performance_schema", "sys"):
        assert f"'{schema}'" in where
    assert "'system'" in where and "'temp'" in where and "table_catalog IS NULL" in where
    # an explicit filter wins
    assert WE(str(db), schemas=["analytics"])._where().count("'analytics'") == 1
    assert "lower(table_catalog) = 'prod'" in WE(str(db), database="prod")._where()


def test_duckdb_schema_has_no_system_objects(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from datagraph import NodeType
    from datagraph.extractors.warehouse_extractor import WarehouseExtractor as WE

    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute("CREATE TABLE dim_customer (customer_id INTEGER PRIMARY KEY, country VARCHAR)")
    con.execute("CREATE TABLE fact_sales (sale_id INTEGER PRIMARY KEY, customer_id INTEGER, amount DOUBLE)")
    con.execute("CREATE VIEW v_country AS SELECT c.country, SUM(s.amount) amount FROM fact_sales s "
                "JOIN dim_customer c ON c.customer_id = s.customer_id GROUP BY 1")
    graph = WE(con, dialect="duckdb").extract()
    con.close()
    ids = {n.id for n in graph.nodes(NodeType.TABLE) + graph.nodes(NodeType.VIEW)}
    assert any(i.endswith("main.dim_customer") for i in ids)
    assert not [i for i in ids if "duckdb_" in i or "sqlite_" in i or i.startswith("table:system.")]


def test_mysql_catalog_placeholder_and_quoting():
    """MySQL reports table_catalog='def' and rejects double-quoted identifiers - both were bugs
    found by the live-engine CI job."""
    from datagraph.extractors.warehouse_extractor import _table_id
    from datagraph.profiling import _q, _quote_char, _relation_sql

    # 'def' is a placeholder, not a catalog
    assert _table_id("def", "analytics", "orders") == "table:analytics.orders"
    assert _table_id("prod", "analytics", "orders") == "table:prod.analytics.orders"
    assert _table_id(None, "analytics", "orders") == "table:analytics.orders"

    class FakeMySQL:
        __module__ = "pymysql.connections"

    class FakePostgres:
        __module__ = "psycopg2.extensions"

    assert _quote_char(FakeMySQL()) == "`"
    assert _quote_char(FakePostgres()) == '"'
    assert _q("Mixed Case", "`") == "`Mixed Case`"
    assert _q('we"ird') == '"we""ird"'
    assert _q("back`tick", "`") == "`back``tick`"
    # mixed-case parts are quoted with the engine's own character
    assert _relation_sql("table:analytics.Orders", None, False, "`") == "analytics.`Orders`"
    assert _relation_sql("table:analytics.orders", None, False, "`") == "analytics.orders"


def test_engine_detection_sees_through_sqlalchemy_wrappers():
    """connect() returns a SQLAlchemy _ConnectionFairy: sniffing type(conn).__module__ reports
    'sqlalchemy' and silently picked the wrong quote character (MySQL then read "col" as the
    string 'col') and disabled every usage query. Found by the live-engine CI job."""
    from datagraph.extractors.warehouse_extractor import dbapi_connection, engine_name
    from datagraph.profiling import _quote_char
    from datagraph.usage import detect_dialect

    class FakePyMySQL:
        __module__ = "pymysql.connections"

    class FakeFairy:                      # what sqlalchemy's raw_connection() hands back
        __module__ = "sqlalchemy.pool.base"

        def __init__(self, inner):
            self.dbapi_connection = inner
            self.driver_connection = inner

    wrapped = FakeFairy(FakePyMySQL())
    assert isinstance(dbapi_connection(wrapped), FakePyMySQL)
    assert engine_name(wrapped) == "mysql"
    assert detect_dialect(wrapped) == "mysql"
    assert _quote_char(wrapped) == "`"

    class FakePsycopg:
        __module__ = "psycopg2.extensions"

    assert engine_name(FakeFairy(FakePsycopg())) == "postgres"
    assert _quote_char(FakeFairy(FakePsycopg())) == '"'
    # a bare (unwrapped) connection still works, and an unknown driver never raises
    assert engine_name(FakePyMySQL()) == "mysql"
    assert engine_name(object()) == "builtins"


def test_sqlite_detected_through_a_wrapper(tmp_path):
    """A SQLAlchemy-wrapped SQLite connection must still use the sqlite code path."""
    import sqlite3 as _sqlite3

    from datagraph.extractors.warehouse_extractor import dbapi_connection

    class FakeFairy:
        __module__ = "sqlalchemy.pool.base"

        def __init__(self, inner):
            self.dbapi_connection = inner

    raw = _sqlite3.connect(":memory:")
    assert isinstance(dbapi_connection(FakeFairy(raw)), _sqlite3.Connection)
    raw.close()


def test_sqlglot_dialect_is_inferred_from_the_connection():
    """View lineage is parsed with the engine's own dialect, or it silently disappears.

    MySQL quotes identifiers with backticks. Parsed as generic SQL, every MySQL view definition
    fails on its first token - and because an unparseable view is not a reason to fail an
    extraction, the failure shows up as *missing lineage* rather than as an error.
    """
    import sqlite3

    from datagraph.extractors.warehouse_extractor import WarehouseExtractor, sqlglot_dialect

    connection = sqlite3.connect(":memory:")
    assert sqlglot_dialect(connection) == "sqlite"
    assert WarehouseExtractor(connection).dialect == "sqlite"
    # an explicit dialect always wins
    assert WarehouseExtractor(connection, dialect="duckdb").dialect == "duckdb"


def test_backtick_view_sql_needs_the_mysql_dialect():
    """The parse that was failing, pinned directly - no MySQL server required."""
    sqlglot = pytest.importorskip("sqlglot")

    mysql_view = "select `c`.`country` AS `country` from `shop`.`dim_customer` `c`"
    with pytest.raises(Exception):
        sqlglot.parse_one(mysql_view)
    assert sqlglot.parse_one(mysql_view, read="mysql") is not None
