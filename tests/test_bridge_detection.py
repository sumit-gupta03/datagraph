"""Automatic code<->data bridge: SQL embedded in Python, and table-alias linking."""

import json
import textwrap

from impactgraph import EdgeType, ImpactGraph, PythonExtractor, DbtExtractor
from impactgraph.cli import main
from impactgraph.extractors.sql_in_code import looks_like_sql, sql_tables


def test_sql_tables_reads_writes_and_placeholders():
    r, w, certain = sql_tables("SELECT a.id, b.x FROM analytics.orders a JOIN raw.customers b ON a.cid = b.id")
    assert r == {"analytics.orders", "raw.customers"} and w == set() and certain
    r, w, certain = sql_tables("INSERT INTO analytics.fact_booking SELECT * FROM staging.bookings")
    assert w == {"analytics.fact_booking"} and r == {"staging.bookings"}
    r, w, certain = sql_tables("select * from {schema}.events where day = %s")
    assert not certain and r == set()  # placeholder table cannot be named
    assert not looks_like_sql("hello world") and looks_like_sql("SELECT id FROM t1 WHERE x = 1")


def test_python_sql_strings_become_table_edges(tmp_path):
    (tmp_path / "reports.py").write_text(textwrap.dedent('''
        import db

        def revenue():
            return db.query("SELECT booking_id, amount FROM analytics.fact_booking WHERE amount > 0")

        def archive(day):
            sql = f"INSERT INTO archive.bookings SELECT * FROM analytics.fact_booking WHERE day = '{day}'"
            return db.execute(sql)
    '''), encoding="utf-8")
    graph = PythonExtractor(tmp_path).extract()
    edges = {(e.src, e.dst, e.type, e.provenance) for e in graph.edges()}
    assert ("func:reports.py::revenue", "table:analytics.fact_booking", EdgeType.DEPENDS_ON, "extracted") in edges
    assert ("func:reports.py::archive", "table:archive.bookings", EdgeType.WRITES_TO, "inferred") in edges  # f-string -> inferred
    # a table change reaches the function that reads it
    assert "func:reports.py::revenue" in graph.impact("table:analytics.fact_booking")
    # and the function that writes a table is upstream of it
    assert "func:reports.py::archive" in graph.upstream("table:archive.bookings")


def test_alias_linking_bridges_code_and_dbt(tmp_path, dbt_manifest):
    (tmp_path / "api.py").write_text(textwrap.dedent('''
        def bookings():
            return run("SELECT * FROM analytics.fact_booking")
    '''), encoding="utf-8")
    graph = ImpactGraph()
    graph.merge(PythonExtractor(tmp_path).extract())
    graph.merge(DbtExtractor(dbt_manifest).extract())  # materializes table:prod.analytics.fact_booking
    assert "func:api.py::bookings" not in graph.impact("dbt:customer")  # not linked yet
    linked = graph.link_table_aliases()
    assert linked >= 1
    # now a change to the upstream dbt model reaches the Python function through the alias
    assert "func:api.py::bookings" in graph.impact("dbt:customer")


def test_cli_build_links_aliases_by_default(tmp_path, dbt_manifest, capsys):
    (tmp_path / "api.py").write_text('def f():\n    return q("SELECT 1 FROM analytics.fact_booking")\n', encoding="utf-8")
    gp = tmp_path / "g.json"
    assert main(["build", "--repo", str(tmp_path), "--dbt-manifest", str(dbt_manifest), "-o", str(gp)]) == 0
    out = capsys.readouterr().out
    assert "linked" in out
    assert main(["impact", "dbt:customer", "--graph", str(gp), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "func:api.py::f" in payload["affected"]
