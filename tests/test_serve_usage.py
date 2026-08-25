"""The local viewer (datagraph serve) and query-log usage statistics."""

import json
import sqlite3
import threading
import urllib.request
from urllib.error import HTTPError

import pytest

from datagraph import WarehouseExtractor
from datagraph.cli import main
from datagraph.knowledge import build_wiki, context
from datagraph.mcp_server import build_tools
from datagraph.serve import create_server
from datagraph.usage import detect_dialect, unused_tables, usage_stats, usage_summary


# --------------------------------------------------------------------- usage

class _FakeCursor:
    def __init__(self, rows, fail=False):
        self.rows, self.fail, self.sql = rows, fail, None

    def execute(self, sql):
        self.sql = sql
        if self.fail:
            raise RuntimeError("SQL compilation error: Object 'SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY' does not exist")

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class _FakeConnection:
    """Pretends to be a Snowflake connection: the module name drives dialect detection."""

    def __init__(self, rows, fail=False):
        self._cursor = _FakeCursor(rows, fail)

    def cursor(self):
        return self._cursor


@pytest.fixture
def warehouse(tmp_path):
    db = tmp_path / "w.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE dim_customer (customer_id INTEGER PRIMARY KEY, email TEXT);
        CREATE TABLE fact_sales (sale_id INTEGER PRIMARY KEY,
                                 customer_id INTEGER REFERENCES dim_customer(customer_id), amount REAL);
        CREATE TABLE legacy_scratch (id INTEGER PRIMARY KEY, note TEXT);
        """
    )
    con.executemany("INSERT INTO dim_customer VALUES (?,?)", [(i, f"u{i}@x.com") for i in range(1, 6)])
    con.executemany("INSERT INTO fact_sales VALUES (?,?,?)", [(i, i, float(i)) for i in range(1, 21)])
    con.commit(); con.close()
    return db


def test_usage_attaches_counts_and_finds_unused(warehouse):
    graph = WarehouseExtractor(str(warehouse)).extract()
    rows = [("fact_sales", 120, "2026-08-24 10:00:00"), ("dim_customer", 8, "2026-08-01 09:00:00")]
    results = usage_stats(_FakeConnection(rows), graph, dialect="snowflake", days=30)

    assert results["table:fact_sales"]["queries"] == 120
    assert results["table:fact_sales"]["window_days"] == 30
    assert graph.get_node("table:dim_customer").meta["usage"]["last_query"].startswith("2026-08-01")
    # a table nobody queried is still measured, so "0" means 0 and not "unknown"
    assert results["table:legacy_scratch"]["queries"] == 0

    unused = unused_tables(graph)
    ids = {r["id"]: r for r in unused}
    assert "table:legacy_scratch" in ids and ids["table:legacy_scratch"]["safe_to_drop"] is True
    assert "table:fact_sales" not in ids
    assert "120 queries in 30d" in usage_summary(graph.get_node("table:fact_sales"))
    assert usage_summary(graph.get_node("table:legacy_scratch")).startswith("0 queries")


def test_usage_matches_qualified_names(warehouse):
    """The engine reports schema.table; the graph may hold db.schema.table (or the bare name)."""
    graph = WarehouseExtractor(str(warehouse)).extract()
    results = usage_stats(_FakeConnection([("analytics.fact_sales", 5, None)]), graph, dialect="snowflake")
    assert results["table:fact_sales"]["queries"] == 5


def test_usage_degrades_on_unsupported_engine_and_missing_grant(warehouse):
    graph = WarehouseExtractor(str(warehouse)).extract()
    logged = []
    assert usage_stats(_FakeConnection([]), graph, dialect="sqlite", log=logged.append) == {}
    assert "no query log" in logged[0]

    logged.clear()
    assert usage_stats(_FakeConnection([], fail=True), graph, dialect="snowflake", log=logged.append) == {}
    assert "could not read the query log" in logged[0]
    assert not any(n.meta.get("usage") for n in graph.nodes())      # nothing half-written


def test_detect_dialect():
    assert detect_dialect(sqlite3.connect(":memory:")) == "sqlite"
    assert detect_dialect(_FakeConnection([])) in ("tests", "test_serve_usage")  # module-derived, never raises


def test_usage_in_context_report_and_mcp(warehouse, tmp_path):
    graph = WarehouseExtractor(str(warehouse)).extract()
    usage_stats(_FakeConnection([("fact_sales", 42, None)]), graph, dialect="snowflake")
    assert "usage: 42 queries" in context(graph, "fact_sales")

    build_wiki(graph, tmp_path / "kb")
    report = (tmp_path / "kb" / "GRAPH_REPORT.md").read_text(encoding="utf-8")
    assert "## Never queried" in report and "legacy_scratch" in report

    gp = tmp_path / "g.json"
    graph.save(gp)
    tools = build_tools(str(gp))
    payload = tools["usage"]()
    assert payload["usage"]["table:fact_sales"]["queries"] == 42
    assert any(r["id"] == "table:legacy_scratch" for r in payload["unused"])


# --------------------------------------------------------------------- serve

@pytest.fixture
def server(warehouse, tmp_path):
    graph = WarehouseExtractor(str(warehouse)).extract()
    usage_stats(_FakeConnection([("fact_sales", 7, None)]), graph, dialect="snowflake")
    gp = tmp_path / "g.json"
    graph.save(gp)
    srv, url = create_server(str(gp), host="127.0.0.1", port=0)   # port 0 = pick a free one
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield url
    srv.shutdown()
    srv.server_close()


def _get(url, parse_json=True):
    with urllib.request.urlopen(url, timeout=10) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if parse_json else body


def test_viewer_serves_page_and_apis(server):
    page = _get(server + "/", parse_json=False)
    assert "<title>datagraph</title>" in page and "/api/search" in page

    rows = _get(server + "/api/search?q=fact")
    assert rows and rows[0]["id"] == "table:fact_sales"
    assert all(r["type"] == "table" for r in _get(server + "/api/search?type=table"))

    node = _get(server + "/api/node/table:fact_sales")
    assert node["name"] == "fact_sales" and "columns:" in node["context"] and "7 queries" in node["usage"]

    report = _get(server + "/api/report")
    assert report["nodes"] and report["edges"] and "table" in report["types"]
    assert any(r["id"] == "table:legacy_scratch" for r in report["unused_tables"])

    model = _get(server + "/api/model")
    assert "mermaid" in model and "facts" in model

    lineage = _get(server + "/lineage/table:fact_sales", parse_json=False)
    assert "<html" in lineage.lower() and "fact_sales" in lineage
    assert "<html" in _get(server + "/graph", parse_json=False).lower()


def test_viewer_is_read_only_and_reports_unknown_routes(server):
    with pytest.raises(HTTPError) as err:
        _get(server + "/api/node/table:does_not_exist")
    assert err.value.code == 404

    with pytest.raises(HTTPError) as err:
        _get(server + "/nope")
    assert err.value.code == 404

    request = urllib.request.Request(server + "/api/search", data=b"{}", method="POST")
    with pytest.raises(HTTPError) as err:                      # no write path exists
        urllib.request.urlopen(request, timeout=10)
    assert err.value.code in (400, 405, 501)


def test_serve_cli_rejects_missing_graph(tmp_path, capsys):
    assert main(["serve", "--graph", str(tmp_path / "nope.json")]) == 2
    assert "not found" in capsys.readouterr().err
