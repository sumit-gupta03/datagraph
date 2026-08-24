from pathlib import Path

from datagraph import DbtExtractor
from datagraph.cli import main
from datagraph.knowledge import build_wiki, context
from datagraph.mcp_server import build_tools

MANIFEST = Path(__file__).resolve().parents[1] / "examples" / "jaffle_shop" / "manifest.json"


def test_context_pack(dbt_graph):
    txt = context(dbt_graph, "dim_customer")
    assert txt.startswith("# dim_customer  (dbt_model)")
    assert "upstream" in txt and "dbt:customer" in txt
    assert "downstream" in txt and "dbt:fact_booking" in txt
    assert "if changed: risk" in txt
    assert "No node matches" in context(dbt_graph, "nope_xyz")


def test_context_includes_sql_and_tests_from_manifest():
    graph = DbtExtractor(MANIFEST).extract()
    txt = context(graph, "stg_customers")
    assert "built by (SQL)" in txt and "raw_customers" in txt
    assert "dbt tests:" in txt


def test_wiki_export(tmp_path, dbt_graph):
    stats = build_wiki(dbt_graph, tmp_path / "wiki", title="demo kb")
    assert stats["pages"] >= 5
    assert (tmp_path / "wiki" / "index.md").exists()
    assert (tmp_path / "wiki" / "GRAPH_REPORT.md").exists()
    assert (tmp_path / "wiki" / "llms.txt").exists()
    page = (tmp_path / "wiki" / "nodes" / "dbt_customer.md").read_text(encoding="utf-8")
    assert "# customer" in page and "downstream" in page and "## Links" in page
    report = (tmp_path / "wiki" / "GRAPH_REPORT.md").read_text(encoding="utf-8")
    assert "Hotspots" in report and "Roots" in report and "Leaves" in report
    index = (tmp_path / "wiki" / "index.md").read_text(encoding="utf-8")
    assert "dbt_model" in index and "nodes/dbt_customer.md" in index


def test_wiki_and_context_cli(tmp_path, dbt_manifest, capsys):
    gp = tmp_path / "g.json"
    assert main(["build", "--dbt-manifest", str(dbt_manifest), "-o", str(gp)]) == 0
    capsys.readouterr()
    assert main(["wiki", "--graph", str(gp), "-o", str(tmp_path / "kb")]) == 0
    assert "pages" in capsys.readouterr().out
    assert (tmp_path / "kb" / "index.md").exists()
    assert main(["context", "fact_booking", "--graph", str(gp)]) == 0
    assert "revenue_report" in capsys.readouterr().out


def test_mcp_context_tool(tmp_path, dbt_graph):
    gp = tmp_path / "g.json"
    dbt_graph.save(gp)
    tools = build_tools(str(gp))
    assert "context" in tools
    assert "dbt:customer" in tools["context"]("dim_customer")


def test_dbt_tests_and_sql_are_captured(tmp_path):
    import json

    manifest = {
        "metadata": {"project_name": "t"},
        "nodes": {
            "model.t.m": {"resource_type": "model", "name": "m", "original_file_path": "models/m.sql",
                          "depends_on": {"nodes": []}, "compiled_code": "select 1 as id"},
            "test.t.unique_m_id": {"resource_type": "test", "name": "unique_m_id", "depends_on": {"nodes": ["model.t.m"]}},
            "test.t.not_null_m_id": {"resource_type": "test", "name": "not_null_m_id", "depends_on": {"nodes": ["model.t.m"]}},
        },
        "sources": {}, "exposures": {},
    }
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(manifest), encoding="utf-8")
    g = DbtExtractor(p).extract()
    node = g.get_node("dbt:m")
    assert sorted(node.meta["tests"]) == ["not_null_m_id", "unique_m_id"]
    assert node.meta["sql"] == "select 1 as id"
    txt = context(g, "m")
    assert "dbt tests: 2" in txt and "select 1 as id" in txt


def test_context_reports_ambiguity_with_candidates(dbt_graph):
    """A dbt model and its physical table often share a name - say which ids exist."""
    from datagraph import Node, NodeType

    dbt_graph.add_node(Node(id="table:staging.customer", type=NodeType.TABLE, name="customer"))
    txt = context(dbt_graph, "customer")
    assert "ambiguous" in txt
    assert "dbt:customer" in txt and "table:staging.customer" in txt
    assert "No node matches" in context(dbt_graph, "definitely_not_here")
